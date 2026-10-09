"""
Unit tests for Signal Parser, Normalizer, Validator, and Management Rules.
"""

from decimal import Decimal
import pytest

from app.signal import (
    ManagementRuleDefinition,
    ManagementRuleEngine,
    ManagementType,
    MessageType,
    OrderAction,
    SignalParser,
    SignalValidator,
    SymbolNormalizer,
)


@pytest.fixture
def parser():
    normalizer = SymbolNormalizer(broker_suffix="m")
    return SignalParser(normalizer=normalizer)


@pytest.fixture
def validator():
    return SignalValidator(require_sl=True, require_tp=False)


class TestSignalParserNewOrder:
    def test_parse_v1_fixed_format_signal(self, parser):
        text = """Sell #XAU 4148 - 4160
SL: 4171
TP: Trên 200pips"""
        signal = parser.parse(text)

        assert signal.message_type == MessageType.NEW_ORDER
        assert signal.new_order is not None
        assert signal.new_order.action == OrderAction.SELL
        assert signal.new_order.symbol == "XAUUSDm"
        assert signal.new_order.raw_symbol == "#XAU"
        assert signal.new_order.entry_min == Decimal("4148")
        assert signal.new_order.entry_max == Decimal("4160")
        assert signal.new_order.stop_loss == Decimal("4171")
        assert signal.new_order.take_profit is None
        assert signal.new_order.raw_tp_text == "Trên 200pips"

    def test_parse_buy_single_price_with_numeric_tp(self, parser):
        text = """BUY XAUUSD 4150.5
SL 4140.0
TP 4170.0"""
        signal = parser.parse(text)

        assert signal.message_type == MessageType.NEW_ORDER
        assert signal.new_order is not None
        assert signal.new_order.action == OrderAction.BUY
        assert signal.new_order.symbol == "XAUUSDm"
        assert signal.new_order.entry_min == Decimal("4150.5")
        assert signal.new_order.entry_max == Decimal("4150.5")
        assert signal.new_order.stop_loss == Decimal("4140.0")
        assert signal.new_order.take_profit == Decimal("4170.0")

    def test_parse_vietnamese_keywords(self, parser):
        text = """Mua #GOLD 4150
Dừng lỗ: 4140
Chốt lời: 4180"""
        signal = parser.parse(text)

        assert signal.message_type == MessageType.NEW_ORDER
        assert signal.new_order.action == OrderAction.BUY
        assert signal.new_order.symbol == "XAUUSDm"
        assert signal.new_order.stop_loss == Decimal("4140")
        assert signal.new_order.take_profit == Decimal("4180")


class TestManagementRules:
    def test_parse_combo_management_message(self, parser):
        text = "#XAU Kèo chạy rồi ae +120pips chốt bớt dịch stoploss nha"
        signal = parser.parse(text)

        assert signal.message_type == MessageType.POSITION_MANAGEMENT
        assert signal.symbol == "XAUUSDm"
        assert len(signal.management_actions) == 2

        action_types = [a.action_type for a in signal.management_actions]
        assert ManagementType.PARTIAL_CLOSE in action_types
        assert ManagementType.MOVE_SL in action_types

        # Verify default percentage
        partial = next(a for a in signal.management_actions if a.action_type == ManagementType.PARTIAL_CLOSE)
        assert partial.percentage == Decimal("50")

    def test_parse_explicit_percentage(self, parser):
        text = "#XAU chốt bớt 30% dời sl về entry"
        signal = parser.parse(text)

        assert signal.message_type == MessageType.POSITION_MANAGEMENT
        partial = next(a for a in signal.management_actions if a.action_type == ManagementType.PARTIAL_CLOSE)
        assert partial.percentage == Decimal("30")

    def test_parse_single_close(self, parser):
        text = "#XAU chốt nha ae"
        signal = parser.parse(text)

        assert signal.message_type == MessageType.POSITION_MANAGEMENT
        assert len(signal.management_actions) == 1
        assert signal.management_actions[0].action_type == ManagementType.CLOSE
        assert signal.management_actions[0].percentage == Decimal("100")

    def test_dynamic_rule_addition(self):
        engine = ManagementRuleEngine()
        # Add new dynamic custom keyword
        engine.add_or_update_rule("khóa lãi", ManagementType.PARTIAL_CLOSE, {"percentage": Decimal("40")})

        parser = SignalParser(rule_engine=engine)
        signal = parser.parse("#XAU kèo xanh quá khóa lãi đi ae")

        assert signal.message_type == MessageType.POSITION_MANAGEMENT
        assert signal.management_actions[0].action_type == ManagementType.PARTIAL_CLOSE
        assert signal.management_actions[0].percentage == Decimal("40")


class TestSignalValidator:
    def test_valid_signals(self, parser, validator):
        buy_text = "BUY #XAU 4150\nSL 4140"
        sig = parser.parse(buy_text)
        valid, msg = validator.validate_signal(sig)
        assert valid is True

        sell_text = "SELL #XAU 4150\nSL 4160"
        sig = parser.parse(sell_text)
        valid, msg = validator.validate_signal(sig)
        assert valid is True

    def test_invalid_sl_direction(self, parser, validator):
        # BUY but SL is higher than entry!
        bad_buy = "BUY #XAU 4150\nSL 4160"
        sig = parser.parse(bad_buy)
        valid, msg = validator.validate_signal(sig)
        assert valid is False
        assert "must be below entry" in msg

        # SELL but SL is lower than entry!
        bad_sell = "SELL #XAU 4150\nSL 4140"
        sig = parser.parse(bad_sell)
        valid, msg = validator.validate_signal(sig)
        assert valid is False
        assert "must be above entry" in msg

    def test_missing_sl_when_required(self, parser, validator):
        no_sl = "BUY #XAU 4150"
        sig = parser.parse(no_sl)
        valid, msg = validator.validate_signal(sig)
        assert valid is False
        assert "Stop loss is required" in msg
