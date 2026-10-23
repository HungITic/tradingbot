"""
Validation rules for parsed signal data structures.
"""

from __future__ import annotations

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
            return False, "Missing entry price"
        prices = entries + [p for p in (order.stop_loss, order.take_profit, order.take_profit_2) if p is not None]
        if any(not p.is_finite() or p <= 0 for p in prices):
            return False, "Prices must be finite and positive"

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

        # Every target must be profitable for every entry in the zone.
        for tp in (order.take_profit, order.take_profit_2):
            if tp is None:
                continue
            if order.action == OrderAction.BUY and tp <= max(entries):
                return False, f"BUY Take profit ({tp}) must be above all entries ({max(entries)})"
            if order.action == OrderAction.SELL and tp >= min(entries):
                return False, f"SELL Take profit ({tp}) must be below all entries ({min(entries)})"
        if order.take_profit is not None and order.take_profit_2 is not None:
            if order.action == OrderAction.BUY and order.take_profit_2 <= order.take_profit:
                return False, "BUY TP2 must be above TP1"
            if order.action == OrderAction.SELL and order.take_profit_2 >= order.take_profit:
                return False, "SELL TP2 must be below TP1"

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
            for action in signal.management_actions:
                if action.percentage is not None and action.action_type.value == "PARTIAL_CLOSE":
                    if not action.percentage.is_finite() or not 0 <= action.percentage <= 100:
                        return False, "Tỷ lệ chốt một phần phải nằm trong khoảng 0 đến 100%"
                if action.price is not None and (not action.price.is_finite() or action.price <= 0):
                    return False, "Giá SL đích không hợp lệ"
            return True, "Valid"

        return True, "Valid"
