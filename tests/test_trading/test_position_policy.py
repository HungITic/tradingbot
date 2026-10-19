"""Trading-policy regressions: scope, TP phases, precision and failure recovery."""
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.broker.base import PositionInfo, SymbolInfo, TickInfo, OrderResult
from app.signal.models import ManagementType, MessageType
from app.signal.parser import SignalParser
from app.signal.rules import ManagementRuleEngine, ManagementRuleDefinition
from app.trading.risk import RiskConfig
from app.trading.service import TradingService


def position(ticket=1, entry='2650', volume='.10', side='BUY', sl=None):
    return PositionInfo(ticket, 'XAUUSD', D(volume), D(entry), D('2670'),
                        D(sl) if sl is not None else None, D('2690'), D('5'), D('0'), D('0'), side,
                        datetime.now(timezone.utc))


@pytest.fixture
def svc():
    broker = AsyncMock()
    broker.get_positions.return_value = []
    broker.get_pending_orders.return_value = []
    broker.get_history_deals.return_value = []
    broker.get_symbol_info.return_value = SymbolInfo('XAUUSD', 3, D('.001'), D('.01'), D('100'), D('.01'), D('100'))
    broker.get_tick.side_effect = lambda symbol: TickInfo(symbol, D('2670'), D('2670.1'), datetime.now(timezone.utc))
    broker.close_position.return_value = OrderResult(True, ticket=1, comment='Closed')
    broker.modify_position.side_effect = lambda ticket, **kwargs: OrderResult(True, ticket=ticket, comment='Modified')
    broker.cancel_order.return_value = OrderResult(True, ticket=2)
    service = TradingService(broker=broker, risk_config=RiskConfig())
    service.position_orders = AsyncMock(return_value={})
    service.management_history = AsyncMock(return_value=(set(), set()))
    service._insert_management_action = AsyncMock()
    service.set_signal_status = AsyncMock()
    service.notify = AsyncMock()
    service._update_db_order_status = AsyncMock()
    return service


def tp_position(svc, volume='.10', tp1='2660', tp2='2690'):
    pos = position(volume=volume, sl='2640')
    svc.broker.get_positions.return_value = [pos]
    order = SimpleNamespace(mt5_ticket=1, signal_id=10, volume=D(volume),
                            tp1=D(tp1), tp2=D(tp2) if tp2 else None)
    svc.position_orders.return_value = {1: order}
    return pos, order


@pytest.mark.asyncio
async def test_be_uses_own_entry_even_when_siblings_share_setup(svc):
    a, b, unrelated, opposite = position(1, '2650', '.01'), position(2, '2640', '.02'), position(3, '2600', '1'), position(4, '2750', '1', 'SELL')
    svc.broker.get_positions.return_value = [a, b, unrelated, opposite]
    svc._signal_db_id_to_tickets = {10: [1, 2, 4], 11: [3]}
    result = await svc.move_to_breakeven(1)
    assert result.success
    svc.broker.modify_position.assert_awaited_once_with(1, sl=D('2650'))


@pytest.mark.asyncio
async def test_unknown_setup_falls_back_to_ticket_entry(svc):
    svc.broker.get_positions.return_value = [position(1, '2650'), position(2, '2600')]
    await svc.move_to_breakeven(1)
    svc.broker.modify_position.assert_awaited_once_with(1, sl=D('2650'))


@pytest.mark.asyncio
async def test_ticket_mode_ignores_sibling_entries(svc):
    svc.risk_config.breakeven_mode = 'ticket'
    svc._signal_db_id_to_tickets = {10: [1, 2]}
    svc.broker.get_positions.return_value = [position(1, '2650'), position(2, '2600')]
    await svc.move_to_breakeven(1)
    svc.broker.modify_position.assert_awaited_once_with(1, sl=D('2650'))


@pytest.mark.asyncio
@pytest.mark.parametrize('side,entry,expected', [('BUY', '2650', '2651'), ('SELL', '2700', '2699')])
async def test_positive_offset_follows_trade_direction(svc, side, entry, expected):
    svc.risk_config.breakeven_offset_price = D('1')
    svc.broker.get_positions.return_value = [position(side=side, entry=entry)]
    await svc.move_to_breakeven(1)
    svc.broker.modify_position.assert_awaited_once_with(1, sl=D(expected))


@pytest.mark.asyncio
@pytest.mark.parametrize('side,entry,sl', [('BUY', '2650', '2660'), ('SELL', '2700', '2690')])
async def test_automatic_be_never_weakens_existing_sl(svc, side, entry, sl):
    svc.broker.get_positions.return_value = [position(side=side, entry=entry, sl=sl)]
    result = await svc.move_to_breakeven(1)
    assert result.success and result.raw['unchanged']
    svc.broker.modify_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_tp1_uses_configured_percentage_and_respects_be_off(svc):
    tp_position(svc)
    svc.risk_config.default_partial_close_percent = D('30')
    svc.risk_config.auto_breakeven_on_partial = False
    await svc._check_tp_milestones()
    svc.broker.close_position.assert_awaited_once_with(1, D('.03'))
    svc.broker.modify_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_tp1_off_does_not_disable_final_tp(svc):
    pos, _ = tp_position(svc)
    svc.risk_config.tp1_enabled = False
    await svc._check_tp_milestones()
    svc.broker.close_position.assert_not_awaited()
    pos.current_price = D('2700')
    await svc._check_tp_milestones()
    svc.broker.close_position.assert_awaited_once_with(1, D('.10'))


@pytest.mark.asyncio
async def test_single_tp_is_only_full_exit(svc):
    tp_position(svc, tp2=None)
    await svc._check_tp_milestones()
    svc.broker.close_position.assert_awaited_once_with(1, D('.10'))
    svc.broker.modify_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_tp1_close_does_not_move_sl_or_mark_done(svc):
    tp_position(svc)
    svc.broker.close_position.return_value = OrderResult(False, comment='rejected')
    await svc._check_tp_milestones()
    svc.broker.modify_position.assert_not_awaited()
    assert 1 not in svc._tp1_completed_tickets
    assert 1 not in svc._tp1_processed_tickets
    svc.broker.close_position.return_value = OrderResult(True)
    await svc._check_tp_milestones()
    assert svc.broker.close_position.await_count == 2


@pytest.mark.asyncio
async def test_tp1_retries_failed_be_without_repeating_partial_close(svc):
    tp_position(svc)
    svc.broker.modify_position.side_effect = [OrderResult(False, comment='retry'), OrderResult(True)]
    await svc._check_tp_milestones()
    assert 1 not in svc._tp1_completed_tickets
    await svc._check_tp_milestones()
    svc.broker.close_position.assert_awaited_once()
    assert svc.broker.modify_position.await_count == 2
    assert 1 in svc._tp1_completed_tickets


@pytest.mark.asyncio
async def test_tp1_does_not_repeat_after_restart_when_partial_volume_is_reduced(svc):
    pos, order = tp_position(svc, volume='.10')
    pos.volume = D('.05')
    await svc._check_tp_milestones()
    svc.broker.close_position.assert_not_awaited()
    svc.broker.modify_position.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,auto_be,expected_moves', [('skip', True, 0), ('breakeven', False, 0), ('breakeven', True, 1)])
async def test_tp1_minimum_volume_policy(svc, mode, auto_be, expected_moves):
    tp_position(svc, volume='.01')
    svc.risk_config.tp1_min_volume_action = mode
    svc.risk_config.auto_breakeven_on_partial = auto_be
    await svc._check_tp_milestones()
    await svc._check_tp_milestones()
    svc.broker.close_position.assert_not_awaited()
    assert svc.broker.modify_position.await_count == expected_moves


@pytest.mark.asyncio
async def test_partial_honors_nonstandard_broker_volume_step(svc):
    pos = position(volume='1.5')
    svc.broker.get_symbol_info.return_value.volume_min = D('.25')
    svc.broker.get_symbol_info.return_value.volume_step = D('.25')
    result, volume = await svc.partial_one(pos, D('40'))
    assert result.success and volume == D('.50')
    svc.broker.close_position.assert_awaited_once_with(1, D('.50'))


@pytest.mark.asyncio
async def test_partial_close_cancels_pending_max_entry(svc):
    svc._signal_db_id_to_tickets = {10: [1, 2]}
    svc.broker.get_positions.return_value = [position(volume='.05')]
    svc.broker.get_pending_orders.return_value = [SimpleNamespace(ticket=2, comment='bot-max-entry')]
    assert await svc.cancel_dual_entry_pending_orders_on_close(1) == [2]
    svc.broker.cancel_order.assert_awaited_once_with(2)


@pytest.mark.asyncio
async def test_pending_cancellation_can_be_disabled_and_failure_is_retryable(svc):
    svc._signal_db_id_to_tickets = {10: [1, 2]}
    svc.broker.get_pending_orders.return_value = [SimpleNamespace(ticket=2, comment='bot-max-entry')]
    svc.risk_config.auto_cancel_pending_on_full_close = False
    await svc.cancel_dual_entry_pending_orders_on_close(1)
    svc.broker.cancel_order.assert_not_awaited()
    svc.risk_config.auto_cancel_pending_on_full_close = True
    svc.broker.cancel_order.side_effect = [OrderResult(False), OrderResult(True)]
    assert await svc.cancel_dual_entry_pending_orders_on_close(1) == []
    assert 2 not in svc._cancelled_dual_entry_tickets
    assert await svc.cancel_dual_entry_pending_orders_on_close(1) == [2]


@pytest.mark.asyncio
async def test_reconcile_does_not_guess_that_missing_sibling_is_closed(svc):
    svc._signal_db_id_to_tickets = {10: [1, 2]}
    svc.broker.get_pending_orders.return_value = [SimpleNamespace(ticket=2, comment='bot-max-entry')]
    assert await svc._reconcile_dual_entry_pending_orders() == []
    svc.broker.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_sl_price_is_used_instead_of_be(svc):
    svc.broker.get_positions.return_value = [position()]
    parsed = SignalParser().parse('XAUUSD dời SL về 2662')
    assert parsed.management_actions[0].price == D('2662')
    await svc.process_management_signal(parsed, 10, None, None)
    svc.broker.modify_position.assert_awaited_once_with(1, sl=D('2662'))


@pytest.mark.asyncio
async def test_close_buy_does_not_close_sell(svc):
    svc.broker.get_positions.return_value = [position(1), position(2, '2700', side='SELL')]
    parsed = SignalParser().parse('XAUUSD đóng buy')
    await svc.process_management_signal(parsed, 10, None, None)
    svc.broker.close_position.assert_awaited_once_with(1, D('.10'))


@pytest.mark.asyncio
async def test_generic_partial_uses_runtime_percentage_not_fixed_fifty(svc):
    svc.risk_config.default_partial_close_percent = D('30')
    svc.broker.get_positions.return_value = [position()]
    parsed = SignalParser().parse('XAUUSD chốt bớt')
    assert parsed.management_actions[0].percentage is None
    await svc.process_management_signal(parsed, 10, None, None)
    svc.broker.close_position.assert_awaited_once_with(1, D('.03'))


@pytest.mark.asyncio
async def test_ambiguous_close_requires_clear_instruction_even_with_legacy_db_rule(svc):
    rule = ManagementRuleDefinition('dừng', ManagementType.CLOSE)
    parsed = SignalParser(rule_engine=ManagementRuleEngine([rule])).parse('XAUUSD dừng')
    assert svc.signal_problem(parsed)


def test_conflicting_management_instructions_are_not_executed():
    assert SignalParser().parse('XAUUSD chốt hết và chốt bớt').message_type == MessageType.UNKNOWN
    assert SignalParser().parse('XAUUSD dời sl 2660 rồi dời sl 2665').message_type == MessageType.UNKNOWN


@pytest.mark.asyncio
async def test_policy_is_shown_before_execution(svc):
    svc.build_risk_context = AsyncMock()
    svc.broker.get_positions.return_value = [position()]
    result = await svc.preview_signal('XAUUSD chốt bớt')
    assert 'entry riêng từng ticket' in result['text']
    assert 'không tự chuyển về giữa hai entry' in result['text']
    svc.broker.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_be_preview_is_readonly_and_changed_price_requires_new_preview(svc):
    pos = position()
    svc.broker.get_positions.return_value = [pos]
    preview = await svc.breakeven_preview(1)
    assert preview['price'] == D('2650')
    svc.broker.modify_position.assert_not_awaited()
    pos.open_price = D('2652')
    result = await svc.move_to_breakeven(1, expected_price=preview['price'])
    assert not result.success and 'thay đổi' in result.comment
    svc.broker.modify_position.assert_not_awaited()


@pytest.mark.parametrize('explicit,inherit,expected', [('', True, '30'), ('40%', True, '40'), ('', False, '50')])
def test_legacy_generic_rule_percentage_obeys_visible_inheritance_policy(svc, explicit, inherit, expected):
    rule = ManagementRuleDefinition('chốt bớt', ManagementType.PARTIAL_CLOSE, {'percentage': D('50')})
    parser = SignalParser(rule_engine=ManagementRuleEngine([rule]))
    action = parser.parse('XAUUSD chốt bớt ' + explicit).management_actions[0]
    svc.risk_config.default_partial_close_percent = D('30')
    svc.risk_config.generic_partial_uses_default = inherit
    assert svc.partial_percentage(action) == D(expected)


def test_custom_fraction_rule_keeps_its_explicit_percentage(svc):
    rule = ManagementRuleDefinition('chốt một phần ba', ManagementType.PARTIAL_CLOSE, {'percentage': D('33')})
    action = SignalParser(rule_engine=ManagementRuleEngine([rule])).parse('XAUUSD chốt một phần ba').management_actions[0]
    assert svc.partial_percentage(action) == D('33')


def test_negative_sl_target_is_rejected_instead_of_converted_to_be(svc):
    parsed = SignalParser().parse('XAUUSD dời SL về -1')
    assert parsed.management_actions[0].price == D('-1')
    assert svc.signal_problem(parsed)


@pytest.mark.asyncio
async def test_seed_does_not_reenable_disabled_rules():
    from app.config.seeder import _seed_with_session, INITIAL_BOT_CONFIGS
    from app.signal.rules import DEFAULT_RULES
    session = AsyncMock()
    session.add = MagicMock()
    disabled = [SimpleNamespace(enabled=False) for _ in DEFAULT_RULES]
    records = [SimpleNamespace(value=dict(item['value'])) for item in INITIAL_BOT_CONFIGS] + disabled
    results = []
    for record in records:
        result = MagicMock()
        result.scalar_one_or_none.return_value = record
        results.append(result)
    session.execute.side_effect = results
    await _seed_with_session(session)
    assert all(not rule.enabled for rule in disabled)


@pytest.mark.parametrize('text', [
    'XAUUSD chốt bớt entry xấu', 'XAUUSD entry xấu thì chốt bớt',
    'XAUUSD chốt bớt lệnh xấu', 'XAUUSD CHỐT BỚT 30% ENTRY XẤU',
])
def test_bad_entry_keyword_targets_min(text):
    parsed = SignalParser().parse(text)
    partial = next(a for a in parsed.management_actions if a.action_type == ManagementType.PARTIAL_CLOSE)
    assert partial.target == 'MIN_ENTRY'


def test_gold_symbol_without_bad_keyword_does_not_target_min():
    parsed = SignalParser().parse('XAUUSD chốt bớt')
    assert parsed.management_actions[0].target is None


@pytest.mark.asyncio
@pytest.mark.parametrize('side,min_price,max_price', [('BUY', '2650', '2640'), ('SELL', '2700', '2710')])
async def test_bad_entry_closes_configured_fraction_only_on_min(svc, side, min_price, max_price):
    minimum, maximum = position(1, min_price, side=side), position(2, max_price, side=side)
    minimum.comment, maximum.comment = 'bot-min-entry', 'bot-max-entry'
    svc.broker.get_positions.return_value = [maximum, minimum]
    svc.risk_config.default_partial_close_percent = D('40')
    svc.risk_config.auto_breakeven_on_partial = False
    parsed = SignalParser().parse('XAUUSD chốt bớt entry xấu')
    await svc.process_management_signal(parsed, 10, None, None)
    svc.broker.close_position.assert_awaited_once_with(1, D('.04'))
    svc.broker.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_bad_entry_percentage_overrides_config(svc):
    minimum, maximum = position(1), position(2)
    minimum.comment, maximum.comment = 'bot-override-min', 'bot-override-max'
    svc.broker.get_positions.return_value = [minimum, maximum]
    svc.risk_config.default_partial_close_percent = D('40')
    svc.risk_config.auto_breakeven_on_partial = False
    await svc.process_management_signal(SignalParser().parse('XAUUSD chốt bớt 30% entry xấu'), 10, None, None)
    svc.broker.close_position.assert_awaited_once_with(1, D('.03'))


@pytest.mark.asyncio
async def test_bad_entry_uses_saved_branch_after_live_comment_is_lost(svc):
    minimum, maximum = position(1), position(2)
    svc.broker.get_positions.return_value = [minimum, maximum]
    svc.position_orders.return_value = {
        1: SimpleNamespace(comment='bot-min-entry', volume=D('.10'), signal_id=10),
        2: SimpleNamespace(comment='bot-max-entry', volume=D('.10'), signal_id=10),
    }
    await svc.process_management_signal(SignalParser().parse('XAUUSD chốt bớt entry xấu'), 10, None, None)
    svc.broker.close_position.assert_awaited_once_with(1, D('.05'))


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['', 'bot-max-entry', 'bot-market'])
async def test_bad_entry_does_not_fall_back_to_unknown_or_max_branch(svc, role):
    pos = position()
    pos.comment = role
    svc.broker.get_positions.return_value = [pos]
    await svc.process_management_signal(SignalParser().parse('XAUUSD chốt bớt entry xấu dời SL'), 10, None, None)
    svc.broker.close_position.assert_not_awaited()
    svc.broker.modify_position.assert_not_awaited()
    assert svc.set_signal_status.call_args.args[1] == 'REJECTED'


@pytest.mark.asyncio
async def test_bad_entry_reply_selects_only_replied_setup(svc):
    first, second, maximum = position(1), position(2), position(3)
    first.comment = second.comment = 'bot-min-entry'
    maximum.comment = 'bot-max-entry'
    svc.broker.get_positions.return_value = [first, second, maximum]
    svc.get_tickets_for_reply_message = AsyncMock(return_value=[2, 3])
    await svc.process_management_signal(SignalParser().parse('chốt bớt entry xấu'), 10, 99, None)
    svc.broker.close_position.assert_awaited_once_with(2, D('.05'))


@pytest.mark.asyncio
async def test_multiple_min_entries_without_reply_are_not_guessed(svc):
    first, second = position(1), position(2)
    first.comment = second.comment = 'bot-min-entry'
    svc._signal_db_id_to_tickets = {10: [1], 20: [2]}
    svc.broker.get_positions.return_value = [first, second]
    await svc.process_management_signal(SignalParser().parse('XAUUSD chốt bớt entry xấu'), 10, None, None)
    svc.broker.close_position.assert_not_awaited()
    assert 'nhiều kèo' in svc.notify.call_args.args[0]


@pytest.mark.asyncio
async def test_bad_entry_minimum_lot_does_not_close_max_instead(svc):
    first, second = position(1, volume='.01'), position(2)
    first.comment, second.comment = 'bot-min-entry', 'bot-max-entry'
    svc.broker.get_positions.return_value = [first, second]
    await svc.process_management_signal(SignalParser().parse('XAUUSD chốt bớt entry xấu'), 10, None, None)
    svc.broker.close_position.assert_not_awaited()
    svc.broker.modify_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_bad_entry_preview_shows_exact_target_before_execution(svc):
    first, second = position(1), position(2)
    first.comment, second.comment = 'bot-min-entry', 'bot-max-entry'
    svc.broker.get_positions.return_value = [first, second]
    preview = await svc.preview_signal('XAUUSD chốt bớt entry xấu')
    assert preview['can_execute']
    assert 'Chỉ chốt Min Entry: #1; giữ nguyên Max Entry' in preview['text']
    svc.broker.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_tp1_moves_each_position_to_its_own_entry(svc):
    first, second = position(1, '2650', sl='2630'), position(2, '2640', sl='2630')
    svc.broker.get_positions.return_value = [first, second]
    svc.position_orders.return_value = {
        p.ticket: SimpleNamespace(mt5_ticket=p.ticket, signal_id=10, volume=D('.10'), tp1=D('2660'), tp2=D('2690'))
        for p in [first, second]
    }
    await svc._check_tp_milestones()
    assert [(c.args[0], c.kwargs['sl']) for c in svc.broker.modify_position.call_args_list] == [(1, D('2650')), (2, D('2640'))]


@pytest.mark.asyncio
async def test_legacy_setup_config_loads_as_ticket_without_pausing(svc):
    from contextlib import asynccontextmanager
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [SimpleNamespace(value={'breakeven_mode': 'setup'}, key='position_management')]
    session.execute.return_value = result
    @asynccontextmanager
    async def db():
        yield session
    with patch('app.config.database.get_session', db):
        await svc.load_risk_config_from_db()
    assert svc.risk_config.breakeven_mode == 'ticket'
    assert svc.risk_config.enabled
    with pytest.raises(ValueError):
        svc.risk_config.with_updates({'breakeven_mode': 'setup'})


@pytest.mark.asyncio
async def test_bad_entry_without_symbol_uses_only_uniquely_proven_setup(svc):
    minimum, maximum, unrelated = position(1), position(2), position(3)
    minimum.comment, maximum.comment = 'bot-min-entry', 'bot-max-entry'
    svc._signal_db_id_to_tickets = {10: [1, 2], 20: [3]}
    svc.broker.get_positions.return_value = [minimum, maximum, unrelated]
    parsed = SignalParser().parse('chốt bớt entry xấu dời SL')
    await svc.process_management_signal(parsed, 99, None, None)
    svc.broker.close_position.assert_awaited_once_with(1, D('.05'))
    assert {c.args[0] for c in svc.broker.modify_position.call_args_list} == {1, 2}
    svc.broker.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_bad_entry_preview_without_symbol_rejects_multiple_setups(svc):
    first, second = position(1), position(2)
    first.comment = second.comment = 'bot-min-entry'
    svc._signal_db_id_to_tickets = {10: [1], 20: [2]}
    svc.broker.get_positions.return_value = [first, second]
    result = await svc.preview_signal('chốt bớt entry xấu')
    assert not result['can_execute']
    assert 'nhiều kèo' in result['text']
    svc.broker.close_position.assert_not_awaited()


def test_bad_entry_uses_config_even_with_custom_rule_percentage(svc):
    rule = ManagementRuleDefinition('chốt bớt entry xấu', ManagementType.PARTIAL_CLOSE, {'percentage': D('50')})
    parsed = SignalParser(rule_engine=ManagementRuleEngine([rule])).parse('XAUUSD chốt bớt entry xấu')
    svc.risk_config.default_partial_close_percent = D('40')
    svc.risk_config.generic_partial_uses_default = False
    assert parsed.management_actions[0].target == 'MIN_ENTRY'
    assert svc.partial_percentage(parsed.management_actions[0]) == D('40')
