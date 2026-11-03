from decimal import Decimal as D

import pytest

from app.signal.parser import SignalParser
from app.signal.recovery import SignalRecoveryError, recover_order
from app.trading.risk import RiskConfig


RAW = 'SELL XAUUSD 3956 - 3864\nSL:3973\nTP:3943 - 3930'


def recover(raw=RAW, quote='3956', **settings):
    return recover_order(SignalParser().parse(raw).new_order, D(quote), RiskConfig(**settings))


@pytest.mark.parametrize('raw,quote,retained', [
    (RAW, '3956', '3956'),
    ('BUY XAUUSD 3956 - 4864\nSL:3939\nTP:3969 - 3982', '3956', '3956'),
])
def test_entry_recovery_preserves_one_original_price(raw, quote, retained):
    order, decision = recover(raw, quote)
    assert order.entry_min == D(retained) and order.entry_max is None
    assert decision['mode'] == 'single_entry'
    assert order.stop_loss == D('3973' if order.action.value == 'SELL' else '3939')


@pytest.mark.parametrize('raw,field,value', [
    ('SELL XAUUSD 3956 - 3964\nSL:3873\nTP:3943 - 3930', 'stop_loss', '3973'),
    ('SELL XAUUSD 3956 - 3964\nSL:3973\nTP:4943 - 3930', 'take_profit', '3943'),
    ('SELL XAUUSD 3956 - 3964\nSL:3973\nTP:3943 - 4930', 'take_profit_2', '3930'),
    ('BUY XAUUSD 3956 - 3948\nSL:4939\nTP:3969 - 3982', 'stop_loss', '3939'),
])
def test_sl_tp_typos_are_never_inferred_even_if_a_unique_repair_exists(raw, field, value):
    original = SignalParser().parse(raw).new_order
    with pytest.raises(SignalRecoveryError):
        recover_order(original, D('3956'), RiskConfig())
    assert getattr(original, field) != D(value)


@pytest.mark.parametrize('raw,quote,settings', [
    (RAW, '3956', {'signal_recovery_enabled': False}),
    (RAW, '4000', {}),
    (RAW, '3910', {}),  # Equal distance; never break a tie by entry order.
    (RAW.replace('3973', '3873'), '3956', {}),  # More than one broken component.
    (RAW.replace('3973', '4973'), '3956', {}),  # Retained entry still has implausible SL.
    (RAW.replace('XAUUSD', 'BTCUSD'), '3956', {}),
    ('SELL XAUUSD 3956 - 3964\nTP:4943 - 3930', '3956', {}),
    ('BUY XAUUSD 2650\nSL:2660\nTP:2670', '2650', {}),  # Ambiguous SL repair.
    ('SELL XAUUSD 3956 - 3964\nSL:3873\nTP:4943 - 3930', '3956', {}),
])
def test_unsupported_or_ambiguous_recovery_is_rejected(raw, quote, settings):
    with pytest.raises(SignalRecoveryError):
        recover(raw, quote, **settings)


def test_valid_signal_is_unchanged_even_when_wide_or_far_from_quote():
    raw = 'SELL XAUUSD 3956 - 3864\nSL:4973\nTP:3843 - 3830'
    original = SignalParser().parse(raw).new_order
    order, decision = recover(raw, '5000')
    assert order == original and decision is None


@pytest.mark.parametrize('field', ['recovery_max_entry_gap', 'recovery_max_quote_distance',
                                  'recovery_max_sl_distance', 'recovery_max_tp_distance'])
@pytest.mark.parametrize('value', ['0', '-1', 'NaN', 'Infinity'])
def test_recovery_thresholds_require_finite_positive_values(field, value):
    with pytest.raises(ValueError):
        RiskConfig(**{field: value})
