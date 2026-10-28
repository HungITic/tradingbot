"""Slippage limits: exact entry, durable invalidation, cancellation-only and retry."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.broker.base import AccountInfo, SymbolInfo, TickInfo, OrderResult
from app.models.signal import Signal
from app.models.risk import RiskEvent
from app.signal.parser import SignalParser
from app.trading.risk import RiskConfig
from app.trading.service import TradingService

CHAT = -1001234567890


@pytest.fixture
def limits(monkeypatch):
    events = []
    session = AsyncMock()
    result = MagicMock()
    result.rowcount = 1
    result.scalar_one_or_none.return_value = 300
    result.all.return_value = []
    session.execute.return_value = result
    ticket_result = MagicMock()
    ticket_result.all.return_value = [101, 102]
    session.scalars.return_value = ticket_result
    session.scalar.return_value = 1
    added = []
    def add(obj):
        added.append(obj)
        if isinstance(obj, RiskEvent) and obj.event_type == 'LIMIT_INVALIDATED':
            events.append('invalidation_intent')
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
    broker.get_positions.return_value = []
    broker.get_pending_orders.return_value = []
    broker.get_history_deals.return_value = []
    broker.get_symbol_info.return_value = SymbolInfo('XAUUSD', 2, D('.01'), D('.01'), D('100'), D('.01'), D('100'))
    broker.get_tick.return_value = TickInfo('XAUUSD', D('2652'), D('2652'), datetime.now(timezone.utc))
    async def opened(req):
        events.append('open')
        return OrderResult(True, ticket=100+broker.open_order.await_count, retcode=10009, volume=req.volume, price=req.price)
    async def cancelled(ticket):
        events.append('cancel')
        return OrderResult(True, ticket=ticket, retcode=10009)
    broker.open_order.side_effect = opened
    broker.cancel_order.side_effect = cancelled
    service = TradingService(broker, risk_config=RiskConfig(default_lot='1.5', lot_min_entry='1.5',
        lot_max_entry='1.5', max_lot='2', max_open_positions=10, default_partial_close_percent='30'))
    service._record_db_order = AsyncMock()
    service._update_db_order_status = AsyncMock(return_value=True)
    service.set_signal_status = AsyncMock()
    service.notify = AsyncMock()
    service.save_risk_config_to_db = AsyncMock(return_value=True)
    return service, session, result, ticket_result, events


def update(text='chốt bớt'):
    return SignalParser().parse(text)


@pytest.mark.asyncio
@pytest.mark.parametrize('side,entry,second,sl,tp1,tp2,quote', [
    ('BUY','2650','2648','2640','2670','2680','2652'),
    ('SELL','2650','2652','2660','2630','2620','2648'),
])
@pytest.mark.parametrize('dual', [False, True])
async def test_slippage_places_native_limit_legs_at_entry_without_approval(limits, side, entry, second, sl, tp1, tp2, quote, dual):
    svc, session, _, _, _ = limits
    svc.broker.get_tick.return_value = TickInfo('XAUUSD', D(quote), D(quote), datetime.now(timezone.utc))
    raw = f'{side} XAUUSD {entry}' + (f' - {second}' if dual else '') + f'\nSL: {sl}\nTP1: {tp1}\nTP2: {tp2}'
    preview = await svc.preview_signal(raw)
    assert preview['can_execute'] and 'tự đặt' in preview['text']
    await svc.process_raw_message(raw)
    reqs = [call.args[0] for call in svc.broker.open_order.call_args_list]
    assert len(reqs) == (4 if dual else 2)
    assert [r.price for r in reqs] == [D(entry), D(entry)] + ([D(second), D(second)] if dual else [])
    assert all(r.order_type == side+'_LIMIT' for r in reqs)
    assert [r.volume for r in reqs[:2]] == [D('.45'), D('1.05')]
    assert not svc._pending_overrides
    assert any('entry_mode' in str(call.args[0].compile().params) for call in session.execute.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize('quote', ['2650', '2651', '2648'])
async def test_market_kept_when_not_exceeding_adverse_slippage(limits, quote):
    svc, _, _, _, _ = limits
    svc.broker.get_tick.return_value = TickInfo('XAUUSD', D(quote), D(quote), datetime.now(timezone.utc))
    await svc.process_raw_message('BUY XAUUSD 2650\nSL: 2640\nTP: 2670')
    assert svc.broker.open_order.call_args.args[0].order_type == 'BUY'


@pytest.mark.asyncio
async def test_preview_can_place_pullback_limit_even_if_price_already_passed_tp(limits):
    svc, _, _, _, _ = limits
    svc.broker.get_tick.return_value = TickInfo('XAUUSD', D('2700'), D('2700'), datetime.now(timezone.utc))
    preview = await svc.preview_signal('BUY XAUUSD 2650\nSL: 2640\nTP: 2670')
    assert preview['can_execute']
    svc.broker.open_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['chốt bớt', 'chốt bớt entry xấu', 'dời SL về 2655', 'hòa vốn', 'đóng lệnh'])
async def test_related_actions_cancel_only_pending_limits_of_marked_signal(limits, text):
    svc, session, rows, _, events = limits
    rows.all.return_value = [(20, 200, {'entry_mode':'slippage_limit'}, 'XAUUSD')]
    svc.broker.get_pending_orders.return_value = [NS(ticket=101, order_type='BUY_LIMIT'), NS(ticket=102, order_type='BUY_LIMIT'), NS(ticket=999, order_type='BUY_LIMIT')]
    await svc.invalidate_slippage_limits(update(text), CHAT, 300, reply_id=200)
    assert [c.args[0] for c in svc.broker.cancel_order.call_args_list] == [101, 102]
    svc.broker.close_position.assert_not_awaited()
    svc.broker.modify_position.assert_not_awaited()
    intent = events.index('invalidation_intent')
    cancel = events.index('cancel')
    assert 'commit' in events[intent:cancel]
    query = session.execute.call_args.args[0]
    assert 'signal_sources.chat_id' in str(query) and CHAT in query.compile().params.values()


@pytest.mark.asyncio
async def test_partially_filled_native_branch_preserves_open_child(limits):
    svc, _, rows, _, _ = limits
    rows.all.return_value = [(20, 200, {'entry_mode':'slippage_limit'}, 'XAUUSD')]
    svc.broker.get_pending_orders.return_value = [NS(ticket=102, order_type='BUY_LIMIT')]
    await svc.invalidate_slippage_limits(update(), CHAT, 300, reply_id=200)
    svc.broker.cancel_order.assert_awaited_once_with(102)
    svc.broker.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_retry_uses_persisted_intent_and_fresh_broker_snapshot(limits):
    svc, _, _, _, _ = limits
    svc.broker.get_pending_orders.return_value = [NS(ticket=102, order_type='BUY_LIMIT')]
    svc.broker.cancel_order.side_effect = [OrderResult(False, comment='busy'), OrderResult(True)]
    assert await svc.cancel_invalidated_slippage_limits() == []
    svc._update_db_order_status.assert_not_awaited()
    # No in-memory invalidation flag is needed on a new service after restart.
    restarted = TradingService(svc.broker, risk_config=svc.risk_config)
    restarted._update_db_order_status = AsyncMock()
    restarted.notify = AsyncMock()
    assert await restarted.cancel_invalidated_slippage_limits() == [102]
    restarted._update_db_order_status.assert_awaited_once_with(102, 'CANCELLED')


@pytest.mark.asyncio
async def test_latest_setup_without_slippage_does_not_cancel_older_setup(limits):
    svc, session, rows, _, _ = limits
    rows.all.return_value = [(30, 250, {}, 'XAUUSD'), (20, 200, {'entry_mode':'slippage_limit'}, 'XAUUSD')]
    await svc.invalidate_slippage_limits(update(), CHAT, 300)
    svc.broker.cancel_order.assert_not_awaited()
    assert not any(isinstance(c.args[0], RiskEvent) for c in session.add.call_args_list)


@pytest.mark.asyncio
async def test_reply_chain_prefers_nearest_ancestor_signal_not_most_recent_unrelated(limits):
    svc, session, rows, _, _ = limits
    rows.all.return_value = [(20, 200, {'entry_mode':'slippage_limit'}, 'XAUUSD'), (10, 100, {}, 'XAUUSD')]
    svc.broker.get_pending_orders.return_value = [NS(ticket=101, order_type='BUY_LIMIT')]
    await svc.invalidate_slippage_limits(update(), CHAT, 300, reply_id=150, message_path=[300,150,100,200])
    svc.broker.cancel_order.assert_not_awaited()  # Closest source 100 is not a slippage limit.


@pytest.mark.asyncio
async def test_unknown_or_unrelated_new_signal_never_invalidates(limits):
    svc, session, _, _, _ = limits
    for raw in ['Chào AE', 'BUY XAUUSD 2650\nSL: 2640\nTP: 2670']:
        await svc.invalidate_slippage_limits(update(raw), CHAT, 300)
    session.execute.assert_not_awaited()
    svc.broker.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_cancel_result_blocks_further_commands(limits):
    svc, _, rows, _, _ = limits
    rows.all.return_value = [(20, 200, {'entry_mode':'slippage_limit'}, 'XAUUSD')]
    svc.broker.get_pending_orders.return_value = [NS(ticket=101, order_type='BUY_LIMIT'), NS(ticket=102, order_type='BUY_LIMIT')]
    svc.broker.cancel_order.side_effect = None
    svc.broker.cancel_order.return_value = OrderResult(False, raw={'uncertain':True})
    with pytest.raises(RuntimeError):
        await svc.invalidate_slippage_limits(update(), CHAT, 300, reply_id=200)
    assert svc.broker.cancel_order.await_count == 1 and not svc.risk_config.enabled


@pytest.mark.asyncio
async def test_other_risk_limits_still_require_approval_then_keep_slippage_policy(limits):
    svc, _, _, _, _ = limits
    svc.risk_config.max_open_positions = 1
    raw = 'BUY XAUUSD 2650\nSL: 2640\nTP1: 2670\nTP2: 2680'
    await svc.process_raw_message(raw)
    svc.broker.open_order.assert_not_awaited()
    approval_id = next(iter(svc._pending_overrides))
    assert svc._pending_overrides[approval_id]['allowed_rule'] == 'max_positions'
    assert svc._pending_overrides[approval_id]['slippage_limit']
    await svc.execute_override_order(approval_id, 'min')
    assert svc.broker.open_order.await_count == 2
    assert all(c.args[0].order_type == 'BUY_LIMIT' for c in svc.broker.open_order.call_args_list)


@pytest.mark.asyncio
async def test_symbol_mismatch_does_not_invalidate_latest_limit(limits):
    svc, session, rows, _, _ = limits
    rows.all.return_value = [(20, 200, {'entry_mode':'slippage_limit'}, 'EURUSD')]
    await svc.invalidate_slippage_limits(update('XAUUSD chốt bớt'), CHAT, 300)
    svc.broker.cancel_order.assert_not_awaited()
    assert not any(isinstance(c.args[0], RiskEvent) for c in session.add.call_args_list)


@pytest.mark.asyncio
async def test_missing_persistent_policy_blocks_placing_limits(limits):
    svc, _, result, _, _ = limits
    result.rowcount = 0
    await svc.process_raw_message('BUY XAUUSD 2650\nSL: 2640\nTP: 2670')
    svc.broker.open_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_intent_not_persisted_means_no_cancel(limits, monkeypatch):
    from app.config import database
    svc, _, rows, _, _ = limits
    rows.all.return_value = [(20, 200, {'entry_mode':'slippage_limit'}, 'XAUUSD')]
    session = AsyncMock()
    session.execute.return_value = rows
    session.add = MagicMock()
    @asynccontextmanager
    async def broken_commit():
        yield session
        raise RuntimeError('commit failed')
    monkeypatch.setattr(database, 'get_session', broken_commit)
    with pytest.raises(RuntimeError):
        await svc.invalidate_slippage_limits(update(), CHAT, 300, reply_id=200)
    svc.broker.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_preview_allows_cancel_only_action_but_never_cancels(limits):
    svc, session, rows, _, _ = limits
    rows.all.return_value = [(20, 200, {'entry_mode':'slippage_limit'}, 'XAUUSD')]
    svc.broker.get_pending_orders.return_value = [NS(ticket=101, order_type='BUY_LIMIT'), NS(ticket=102, order_type='BUY_LIMIT')]
    preview = await svc.preview_signal('chốt bớt', chat_id=CHAT, message_id=300, reply_to_msg_id=200)
    assert preview['can_execute'] and 'sẽ hủy 2 limit' in preview['text']
    svc.broker.cancel_order.assert_not_awaited()
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_only_action_records_executed_instead_of_skipped(limits):
    svc, _, rows, _, _ = limits
    rows.all.return_value = [(20, 200, {'entry_mode':'slippage_limit'}, 'XAUUSD')]
    pending = [NS(ticket=101, order_type='BUY_LIMIT'), NS(ticket=102, order_type='BUY_LIMIT')]
    svc.broker.get_pending_orders.side_effect = lambda: list(pending)
    async def cancel(ticket):
        pending[:] = [o for o in pending if o.ticket != ticket]
        return OrderResult(True)
    svc.broker.cancel_order.side_effect = cancel
    await svc.process_raw_message('chốt bớt', chat_id=CHAT, message_id=300, reply_to_msg_id=200)
    assert svc.broker.cancel_order.await_count == 2
    assert svc.set_signal_status.call_args.args[1] == 'EXECUTED'
    svc.broker.open_order.assert_not_awaited()
    svc.broker.close_position.assert_not_awaited()
