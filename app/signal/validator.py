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

        entries = [e for e in (order.entry_min, order.entry_max) if e is not None]
        if not entries:
            entries = []

        # Logical SL direction
        if order.stop_loss is not None and entries:
            if order.action == OrderAction.BUY:
                closest_to_sl = min(entries)
                if order.stop_loss >= closest_to_sl:
                    return False, f"BUY Stop loss ({order.stop_loss}) must be below entry ({closest_to_sl})"
            elif order.action == OrderAction.SELL:
                closest_to_sl = max(entries)
                if order.stop_loss <= closest_to_sl:
                    return False, f"SELL Stop loss ({order.stop_loss}) must be above entry ({closest_to_sl})"

        # Logical TP direction
        if order.take_profit is not None and entries:
            if order.action == OrderAction.BUY:
                furthest_entry = max(entries)
                if order.take_profit <= min(entries):
                    return False, f"BUY Take profit ({order.take_profit}) must be above entry ({min(entries)})"
            elif order.action == OrderAction.SELL:
                if order.take_profit >= max(entries):
                    return False, f"SELL Take profit ({order.take_profit}) must be below entry ({max(entries)})"

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
