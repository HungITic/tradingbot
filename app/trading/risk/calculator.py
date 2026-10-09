"""
Lot size calculator with boundary clamping and step precision.
"""

from __future__ import annotations

from decimal import Decimal

from app.signal.models import ParsedSignal
from app.trading.risk.models import RiskConfig


class LotCalculator:
    """Calculates and bounds order volume for execution."""

    def calculate(
        self,
        signal: ParsedSignal,
        config: RiskConfig,
        volume_step: Decimal = Decimal("0.01"),
    ) -> Decimal:
        """
        Calculate executable lot size:
        1. Use signal-specified lot if given, else config.default_lot
        2. Clamp between config.min_lot and config.max_lot
        3. Round to volume_step
        """
        requested = (
            signal.new_order.lot_size
            if signal.new_order and signal.new_order.lot_size
            else config.default_lot
        )

        # Clamping
        lot = max(config.min_lot, min(requested, config.max_lot))

        # Align to volume_step (e.g. 0.01)
        steps = (lot / volume_step).to_integral_value()
        final_lot = steps * volume_step

        return final_lot
