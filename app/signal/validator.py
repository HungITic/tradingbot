"""
Validation rules for parsed signal data structures.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Tuple

from app.signal.models import OrderAction, ParsedNewOrder, ParsedSignal


class SignalValidationError(Exception):
    """Raised when a signal violates structural or logical trade constraints."""

    pass


class SignalValidator:
    """Validates parsed signal parameters before reaching the Risk Engine."""

    def __init__(self, require_sl: bool = True, require_tp: bool = False) -> None:
        self.require_sl = require_sl
        self.require_tp = require_tp

    def validate_new_order(self, order: ParsedNewOrder) -> Tuple[bool, str]:
        """
        Validate logical consistency of entry, SL, and TP.
        Returns (is_valid, reason).
        """
        if not order.symbol:
            return False, "Missing symbol"

        # Check Stop Loss existence if required
        if self.require_sl and order.stop_loss is None:
            return False, "Stop loss is required but missing"

        # Check Take Profit existence if required
        if self.require_tp and order.take_profit is None and not order.raw_tp_text:
            return False, "Take profit is required but missing"

        entry_ref = order.entry_max if order.action == OrderAction.BUY else order.entry_min
        if entry_ref is None:
            entry_ref = order.entry_min or order.entry_max

        # Logical SL direction
        if order.stop_loss is not None and entry_ref is not None:
            if order.action == OrderAction.BUY and order.stop_loss >= entry_ref:
                return False, f"BUY Stop loss ({order.stop_loss}) must be below entry ({entry_ref})"
            if order.action == OrderAction.SELL and order.stop_loss <= entry_ref:
                return False, f"SELL Stop loss ({order.stop_loss}) must be above entry ({entry_ref})"

        # Logical TP direction
        if order.take_profit is not None and entry_ref is not None:
            if order.action == OrderAction.BUY and order.take_profit <= entry_ref:
                return False, f"BUY Take profit ({order.take_profit}) must be above entry ({entry_ref})"
            if order.action == OrderAction.SELL and order.take_profit >= entry_ref:
                return False, f"SELL Take profit ({order.take_profit}) must be below entry ({entry_ref})"

        return True, "Valid"

    def validate_signal(self, signal: ParsedSignal) -> Tuple[bool, str]:
        """Validate entire parsed signal container."""
        if signal.message_type.value == "NEW_ORDER":
            if not signal.new_order:
                return False, "Message marked as NEW_ORDER but new_order data is missing"
            return self.validate_new_order(signal.new_order)

        if signal.message_type.value == "POSITION_MANAGEMENT":
            if not signal.management_actions:
                return False, "Management signal detected but no actions resolved"
            return True, "Valid"

        return True, "Valid"
