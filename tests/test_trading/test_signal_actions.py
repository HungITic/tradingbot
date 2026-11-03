"""Signal-scoped manual exits: provenance, pending fills and confirmation races."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.broker.base import AccountInfo, DealInfo, OrderResult, PendingOrderInfo, PositionInfo
from app.models.risk import RiskEvent
from app.models.trade import Order
from app.trading.risk import RiskConfig
from app.trading.service import TradingService


def position(ticket, volume='.1', comment='bot-min-entry:tp1', identifier=0):
    return PositionInfo(ticket, 'XAUUSD', D(volume), D('3956'), D('3950'), D('3973'), D('3943'),
        D('10'), D('0'), D('0'), 'SELL', datetime.now(timezone.utc), magic=234000,
        comment=comment, identifier=identifier or ticket)


def pending(ticket, volume='.2', comment='bot-max-entry:tp1'):
    return PendingOrderInfo(ticket, 'XAUUSD', 'SELL_LIMIT', D(volume), D('3964'),
                           D('3973'), D('3943'), comment, datetime.now(timezone.utc))


@pytest.fixture
def scenario(monkeypatch):
    positions = [position(11), position(12, '.3', 'bot-min-entry:tp2'), position(21), position(31)]
    pendings = [pending(103), pending(104, '.3', 'bot-max-entry:tp2'), pending(204)]
    rows = []
    for ticket, pos_id, signal_id, comment, volume in [
        (101, 11, 10, 'bot-min-entry:tp1', '.1'), (102, 12, 10, 'bot-min-entry:tp2', '.3'),
        (103, None, 10, 'bot-max-entry:tp1', '.2'), (104, None, 10, 'bot-max-entry:tp2', '.3'),
        (201, 21, 20, 'bot-min-entry:tp1', '.1'), (204, None, 20, 'bot-max-entry:tp1', '.2')]:
        order = Order(id=ticket, mt5_ticket=ticket, signal_id=signal_id, symbol='XAUUSD',
                      volume=D(volume), comment=comment, mt5_order_type=1, magic=234000)
        rows.append((order, pos_id, -1001234567890, 1000 + signal_id))
    events, added, deals = [], [], []
    session = AsyncMock()
    session.add = MagicMock(side_effect=lambda value: added.append(value))
    result = MagicMock()
    result.all.side_effect = lambda: rows
    session.execute.return_value = result
    @asynccontextmanager
    async def db():
        yield session
        events.append('commit')
    monkeypatch.setattr('app.config.database.get_session', db)
    broker = AsyncMock()
    broker.get_positions.side_effect = lambda: list(positions)
    broker.get_pending_orders.side_effect = lambda: list(pendings)
    broker.get_history_deals.side_effect = lambda **kw: list(deals)
    broker.get_account_info.return_value = AccountInfo(1, D('10000'), D('10000'), D('0'),
        D('10000'), 100, 'Demo', 'USC', raw={'margin_mode': 2})
    async def cancel(ticket):
        events.append(('cancel', ticket))
        pendings[:] = [p for p in pendings if p.ticket != ticket]
        return OrderResult(True, ticket=ticket)
    async def close(ticket, volume):
        events.append(('close', ticket))
        positions[:] = [p for p in positions if p.ticket != ticket]
        return OrderResult(True, ticket=ticket, volume=volume)
    broker.cancel_order.side_effect = cancel
    broker.close_position.side_effect = close
    svc = TradingService(broker=broker, risk_config=RiskConfig())
    svc.group_native_positions = AsyncMock(side_effect=lambda items: items)
    svc._insert_management_action = AsyncMock()
    svc._update_db_order_status = AsyncMock(return_value=True)
    return NS(svc=svc, positions=positions, pending=pendings, rows=rows, deals=deals,
              session=session, events=events, added=added, db=db)


async def preview(scenario, mode='all'):
    result = await scenario.svc.preview_signal_exit(10, mode)
    return {k: v for k, v in result.items() if k != 'text'}


def fill(s, ticket=103):
    previous = next(p for p in s.pending if p.ticket == ticket)
    s.pending[:] = [p for p in s.pending if p.ticket != ticket]
    s.positions.append(position(13, str(previous.volume), previous.comment))
    s.deals.append(DealInfo(900, ticket, 13, 'XAUUSD', 'SELL', 'IN', previous.volume,
        previous.price, D('0'), D('0'), D('0'), datetime.now(timezone.utc)))


@pytest.mark.asyncio
async def test_preview_pins_source_account_and_all_native_children_without_mutations(scenario):
    s = scenario
    result = await s.svc.preview_signal_exit(10, 'all')
    assert result['members'] == {'101': '0.1', '102': '0.3', '103': '0.2', '104': '0.3'}
    assert result['account'] == {'login': 1, 'server': 'Demo'}
    assert '2 vị thế · 2 lệnh chờ' in result['text'] and 'Xem signal gốc' in result['text']
    assert not s.added
    s.svc.broker.cancel_order.assert_not_awaited()
    s.svc.broker.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_exit_cancels_before_closing_only_exact_signal_even_when_bot_paused(scenario):
    s = scenario
    args = await preview(s)
    s.svc.risk_config.enabled = False
    message = await s.svc.execute_signal_exit(**args)
    assert [p.ticket for p in s.positions] == [21, 31]
    assert [p.ticket for p in s.pending] == [204]
    mutations = [e for e in s.events if isinstance(e, tuple)]
    assert mutations == [('cancel', 103), ('cancel', 104), ('close', 11), ('close', 12)]
    assert any(isinstance(e, RiskEvent) and e.signal_id == 10 and e.event_type == 'SIGNAL_MANUAL_EXIT' for e in s.added)
    assert 'hủy 2' in message and 'đóng 2' in message
    assert all(call.args[0] == 10 for call in s.svc._insert_management_action.call_args_list)


@pytest.mark.asyncio
async def test_pending_only_does_not_close_existing_or_newly_filled_positions(scenario):
    s = scenario
    args = await preview(s, 'pending')
    fill(s)
    message = await s.svc.execute_signal_exit(**args)
    s.svc.broker.close_position.assert_not_awaited()
    s.svc.broker.cancel_order.assert_awaited_once_with(104)
    assert 13 in [p.ticket for p in s.positions]
    assert 'hủy 1' in message


@pytest.mark.asyncio
@pytest.mark.parametrize('during_cancel', [False, True])
async def test_pending_fill_is_closed_using_opening_order_provenance(scenario, during_cancel):
    s = scenario
    args = await preview(s)
    if during_cancel:
        original = s.svc.broker.cancel_order.side_effect
        async def race(ticket):
            if ticket == 103:
                fill(s)
                return OrderResult(False, ticket=ticket, retcode=10023, comment='Already filled')
            return await original(ticket)
        s.svc.broker.cancel_order.side_effect = race
    else:
        fill(s)
    message = await s.svc.execute_signal_exit(**args)
    assert [p.ticket for p in s.positions] == [21, 31]
    assert [p.ticket for p in s.pending] == [204]
    assert 'đóng 3' in message
    assert sum(c.args[0] == 13 for c in s.svc.broker.close_position.call_args_list) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['new_order', 'increased_volume'])
async def test_widened_scope_requires_new_preview_before_any_mutation(scenario, kind):
    s = scenario
    args = await preview(s)
    if kind == 'new_order':
        s.pending.append(pending(105))
        s.rows.append((Order(id=105, mt5_ticket=105, signal_id=10, symbol='XAUUSD',
                            mt5_order_type=3, comment='bot-max-entry:tp1'), None, -1001234567890, 1010))
    else:
        s.positions[0].volume += D('.1')
    with pytest.raises(ValueError, match='xem trước lại'):
        await s.svc.execute_signal_exit(**args)
    s.svc.broker.close_position.assert_not_awaited()
    s.svc.broker.cancel_order.assert_not_awaited()
    assert not s.added


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['account', 'expired'])
async def test_account_change_or_expired_confirmation_does_not_trade(scenario, kind):
    s = scenario
    args = await preview(s)
    if kind == 'account':
        s.svc.broker.get_account_info.return_value.login = 2
    else:
        args['created_at'] = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
    text = await s.svc.execute_signal_exit(**args)
    assert 'thay đổi' in text or 'hết hạn' in text
    s.svc.broker.close_position.assert_not_awaited()
    s.svc.broker.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_intent_commit_failure_prevents_every_broker_mutation(scenario, monkeypatch):
    s = scenario
    args = await preview(s)
    @asynccontextmanager
    async def db():
        yield s.session
        if s.added:
            raise RuntimeError('commit failed')
    monkeypatch.setattr('app.config.database.get_session', db)
    with pytest.raises(RuntimeError, match='commit failed'):
        await s.svc.execute_signal_exit(**args)
    s.svc.broker.close_position.assert_not_awaited()
    s.svc.broker.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('uncertain,success', [(False, False), (True, False), (True, True)])
async def test_cancel_failure_stops_remaining_operations_without_retry(scenario, uncertain, success):
    s = scenario
    args = await preview(s)
    s.svc.broker.cancel_order.side_effect = None
    s.svc.broker.cancel_order.return_value = OrderResult(success, ticket=103, comment='Failed', raw={'uncertain': uncertain})
    text = await s.svc.execute_signal_exit(**args)
    s.svc.broker.cancel_order.assert_awaited_once_with(103)
    s.svc.broker.close_position.assert_not_awaited()
    assert 'Đã dừng' in text
    assert 'hủy 0' in text
    if uncertain:
        assert not s.svc.risk_config.enabled


@pytest.mark.asyncio
@pytest.mark.parametrize('partial,uncertain,success', [(True, False, True), (False, False, False),
                                                     (False, True, False), (False, True, True)])
async def test_close_failure_or_partial_fill_is_audited_and_not_repeated(scenario, partial, uncertain, success):
    s = scenario
    args = await preview(s)
    s.svc.broker.close_position.side_effect = None
    s.svc.broker.close_position.return_value = OrderResult(success, ticket=11, retcode=10010 if partial else 10006,
        volume=D('.05') if partial else None, comment='Failed', raw={'uncertain': uncertain})
    text = await s.svc.execute_signal_exit(**args)
    s.svc.broker.close_position.assert_awaited_once_with(11, D('.1'))
    assert 'Đã dừng' in text and 'Chưa hoàn tất' in text
    assert any(c.args[2] == 'CLOSE' for c in s.svc._insert_management_action.call_args_list)
    if uncertain:
        assert not s.svc.risk_config.enabled
        assert s.svc._insert_management_action.call_args.args[4] == 'FAILED'


@pytest.mark.asyncio
async def test_repeated_confirmation_cannot_close_other_signal(scenario):
    s = scenario
    args = await preview(s)
    await s.svc.execute_signal_exit(**args)
    s.svc.broker.close_position.reset_mock()
    s.svc.broker.cancel_order.reset_mock()
    assert 'không còn lệnh' in await s.svc.execute_signal_exit(**args)
    s.svc.broker.close_position.assert_not_awaited()
    s.svc.broker.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['missing', 'conflict', 'comment', 'direction'])
async def test_unproven_membership_is_not_selected(scenario, kind):
    s = scenario
    if kind == 'missing':
        s.rows[:] = []
    elif kind == 'conflict':
        s.rows.append((Order(id=999, mt5_ticket=999, signal_id=20, symbol='XAUUSD'), 11, None, None))
    elif kind == 'comment':
        s.positions[0].comment = 'other'
    else:
        s.positions[0].position_type = 'BUY'
    state = await s.svc.linked_signal_state()
    assert 11 not in state['position_orders']


@pytest.mark.asyncio
async def test_stable_position_identifier_resolves_changed_ticket(scenario):
    s = scenario
    s.positions[0].identifier = 11
    s.positions[0].ticket = 11111
    state = await s.svc.linked_signal_state()
    assert state['position_orders'][11111].mt5_ticket == 101


@pytest.mark.asyncio
async def test_setup_cards_keep_signal_ids_links_and_unknown_items_isolated(scenario):
    groups = await scenario.svc.get_setup_cards()
    assert {g['signal_id'] for g in groups} == {10, 20, None}
    selected = next(g for g in groups if g['signal_id'] == 10)
    assert len(selected['positions']) == len(selected['orders']) == 2
    assert selected['message_link'].endswith('/1010')
    unknown = next(g for g in groups if g['signal_id'] is None)
    assert [p.ticket for p in unknown['positions']] == [31]
