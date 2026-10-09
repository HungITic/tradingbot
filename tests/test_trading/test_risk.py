"""
Unit tests for the Risk Engine, Rules, and Lot Calculator.
"""

from datetime import datetime, timezone
from decimal import Decimal
import time
import pytest

from app.broker.base import AccountInfo, PositionInfo
from app.signal.models import MessageType, OrderAction, ParsedNewOrder, ParsedSignal
from app.trading.risk import (
    LotCalculator,
    RiskConfig,
    RiskContext,
    RiskEngine,
    RiskStatus,
)


@pytest.fixture
def dummy_account():
    return AccountInfo(
        login=416446102,
        balance=Decimal("500.0"),
        equity=Decimal("500.0"),
        margin=Decimal("0.0"),
        free_margin=Decimal("500.0"),
        leverage=2000,
        server="Exness-MT5Trial14",
        currency="USD",
    )


@pytest.fixture
def risk_context(dummy_account):
    config = RiskConfig(
        enabled=True,
        max_open_positions=3,
        max_lot=Decimal("0.05"),
        min_lot=Decimal("0.01"),
        default_lot=Decimal("0.02"),
        max_daily_loss_percent=Decimal("10.0"),
        require_sl=True,
        allowed_symbols=["XAUUSDm", "EURUSDm"],
    )
    return RiskContext(
        config=config,
        account=dummy_account,
        open_positions=[],
        today_realized_pnl=Decimal("0.0"),
        broker_connected=True,
    )


@pytest.fixture
def valid_signal():
    order = ParsedNewOrder(
        action=OrderAction.BUY,
        symbol="XAUUSDm",
        raw_symbol="#XAU",
        entry_min=Decimal("4150.0"),
        entry_max=Decimal("4150.0"),
        stop_loss=Decimal("4140.0"),
        take_profit=Decimal("4170.0"),
    )
    return ParsedSignal(
        message_type=MessageType.NEW_ORDER,
        raw_message="BUY #XAU 4150 SL 4140 TP 4170",
        symbol="XAUUSDm",
        new_order=order,
    )


class TestRiskEngine:
    def test_approve_valid_signal(self, valid_signal, risk_context):
        engine = RiskEngine()
        decision = engine.evaluate(valid_signal, risk_context)

        assert decision.approved is True
        assert decision.calculated_lot == Decimal("0.02")
        assert decision.rejection_reason == ""

    def test_reject_when_bot_disabled(self, valid_signal, risk_context):
        risk_context.config.enabled = False
        engine = RiskEngine()
        decision = engine.evaluate(valid_signal, risk_context)

        assert decision.approved is False
        assert "bot_enabled" in decision.rejection_reason

    def test_reject_unauthorized_symbol(self, valid_signal, risk_context):
        valid_signal.symbol = "BTCUSDm"
        valid_signal.new_order.symbol = "BTCUSDm"
        engine = RiskEngine()
        decision = engine.evaluate(valid_signal, risk_context)

        assert decision.approved is False
        assert "symbol_allowed" in decision.rejection_reason

    def test_reject_when_max_positions_reached(self, valid_signal, risk_context):
        # Create 3 dummy open positions
        risk_context.open_positions = [
            PositionInfo(
                ticket=i,
                symbol="XAUUSDm",
                volume=Decimal("0.01"),
                open_price=Decimal("4150.0"),
                current_price=Decimal("4150.0"),
                sl=None,
                tp=None,
                profit=Decimal("0.0"),
                swap=Decimal("0.0"),
                commission=Decimal("0.0"),
                position_type="BUY",
                open_time=datetime.now(timezone.utc),
            )
            for i in range(1, 4)
        ]

        engine = RiskEngine()
        decision = engine.evaluate(valid_signal, risk_context)

        assert decision.approved is False
        assert "max_positions" in decision.rejection_reason

    def test_reject_daily_loss_limit(self, valid_signal, risk_context):
        # 500 balance, loss of 60 USD = 12% > 10%
        risk_context.today_realized_pnl = Decimal("-60.0")
        engine = RiskEngine()
        decision = engine.evaluate(valid_signal, risk_context)

        assert decision.approved is False
        assert "daily_loss_limit" in decision.rejection_reason

    def test_reject_missing_sl_when_required(self, valid_signal, risk_context):
        valid_signal.new_order.stop_loss = None
        engine = RiskEngine()
        decision = engine.evaluate(valid_signal, risk_context)

        assert decision.approved is False
        assert "stop_loss_required" in decision.rejection_reason

    def test_reject_duplicate_signal(self, valid_signal, risk_context):
        # Signal received 5 seconds ago
        risk_context.recent_signals = [
            {"symbol": "XAUUSDm", "action": "BUY", "timestamp": time.time() - 5}
        ]
        engine = RiskEngine()
        decision = engine.evaluate(valid_signal, risk_context)

        assert decision.approved is False
        assert "duplicate_signal" in decision.rejection_reason

    def test_reject_when_broker_disconnected(self, valid_signal, risk_context):
        risk_context.broker_connected = False
        engine = RiskEngine()
        decision = engine.evaluate(valid_signal, risk_context)

        assert decision.approved is False
        assert "broker_connected" in decision.rejection_reason


class TestLotCalculator:
    def test_clamp_to_max_and_min(self):
        calc = LotCalculator()
        config = RiskConfig(min_lot=Decimal("0.01"), max_lot=Decimal("0.05"), default_lot=Decimal("0.02"))

        # Requested too high
        sig_high = ParsedSignal(
            message_type=MessageType.NEW_ORDER,
            raw_message="",
            new_order=ParsedNewOrder(
                action=OrderAction.BUY,
                symbol="XAUUSDm",
                raw_symbol="#XAU",
                lot_size=Decimal("0.50"),
            ),
        )
        assert calc.calculate(sig_high, config) == Decimal("0.05")

        # Requested too low
        sig_low = ParsedSignal(
            message_type=MessageType.NEW_ORDER,
            raw_message="",
            new_order=ParsedNewOrder(
                action=OrderAction.BUY,
                symbol="XAUUSDm",
                raw_symbol="#XAU",
                lot_size=Decimal("0.001"),
            ),
        )
        assert calc.calculate(sig_low, config) == Decimal("0.01")


class TestDynamicConfigUpdates:
    def test_update_config_field(self):
        config = RiskConfig()
        assert config.max_lot == Decimal("0.05")

        ok = config.update_field("max_lot", "0.10")
        assert ok is True
        assert config.max_lot == Decimal("0.10")

        ok = config.update_field("enabled", "false")
        assert ok is True
        assert config.enabled is False

        ok = config.update_field("allowed_symbols", "XAUUSDm, BTCUSDm")
        assert ok is True
        assert config.allowed_symbols == ["XAUUSDM", "BTCUSDM"]

        ok = config.update_field("lot_min_entry", "0.03")
        assert ok is True
        assert config.lot_min_entry == Decimal("0.03")

        ok = config.update_field("lot_max_entry", "0.05")
        assert ok is True
        assert config.lot_max_entry == Decimal("0.05")

        ok = config.update_field("max_slippage_entry", "1.5")
        assert ok is True
        assert config.max_slippage_entry == Decimal("1.5")

        ok = config.update_field("enable_dual_entry", "false")
        assert ok is True
        assert config.enable_dual_entry is False
