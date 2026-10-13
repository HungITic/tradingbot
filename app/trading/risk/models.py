"""
Data models and configurations for the Risk Engine.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RiskStatus(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    WARNING = "WARNING"


class RiskResult(BaseModel):
    """Result from evaluating a single risk rule."""

    rule_name: str
    status: RiskStatus
    reason: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class RiskDecision(BaseModel):
    """Overall decision produced by the Risk Engine."""

    approved: bool
    results: list[RiskResult] = Field(default_factory=list)
    rejection_reason: str = ""
    calculated_lot: Decimal = Decimal("0.01")


class RiskConfig(BaseModel):
    """Dynamic risk configuration — tunable via Personal Bot at runtime."""

    enabled: bool = True
    max_open_positions: int = 5
    max_lot: Decimal = Decimal("0.05")
    min_lot: Decimal = Decimal("0.01")
    default_lot: Decimal = Decimal("0.01")
    max_daily_loss_percent: Decimal = Decimal("10.0")
    require_sl: bool = True
    require_tp: bool = False
    allowed_symbols: list[str] = Field(default_factory=lambda: ["XAUUSD", "XAUUSDm", "XAUUSD.s", "XAUUSDc", "XAUUSD.c"])
    signal_expire_seconds: int = 300
    duplicate_timeout_seconds: int = 10

    # Position Management Rules (Tunable via Personal Bot)
    default_partial_close_percent: Decimal = Decimal("50.0")
    auto_breakeven_on_partial: bool = True
    breakeven_offset_price: Decimal = Decimal("0.0")

    # Dual Entry Setup Rules (Min Entry + Max Entry)
    enable_dual_entry: bool = True
    lot_min_entry: Decimal = Decimal("0.01")
    lot_max_entry: Decimal = Decimal("0.01")
    max_slippage_entry: Decimal = Decimal("1.0")  # Dung sai tối đa khi giá chạy bất lợi (1 giá / 100 points)

    def update_field(self, key: str, value: Any) -> bool:
        """Update any configuration field with validation."""
        if hasattr(self, key):
            current_val = getattr(self, key)
            if isinstance(current_val, Decimal):
                setattr(self, key, Decimal(str(value)))
            elif isinstance(current_val, bool):
                setattr(self, key, str(value).lower() in ("true", "1", "yes", "on"))
            elif isinstance(current_val, int):
                setattr(self, key, int(value))
            elif isinstance(current_val, list):
                if isinstance(value, str):
                    setattr(self, key, [s.strip().upper() for s in value.split(",") if s.strip()])
                else:
                    setattr(self, key, list(value))
            else:
                setattr(self, key, value)
            return True
        return False
