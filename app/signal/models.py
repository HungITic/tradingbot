"""
Domain models for parsed signals and management actions.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class MessageType(str, Enum):
    NEW_ORDER = "NEW_ORDER"
    POSITION_MANAGEMENT = "POSITION_MANAGEMENT"
    UNKNOWN = "UNKNOWN"


class OrderAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ManagementType(str, Enum):
    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    CLOSE = "CLOSE"
    MOVE_SL = "MOVE_SL"
    BREAKEVEN = "BREAKEVEN"
    REENTRY = "REENTRY"


class ParsedNewOrder(BaseModel):
    """Structured new order trade signal."""

    action: OrderAction
    symbol: str  # Normalized symbol (e.g., XAUUSD or XAUUSDm)
    raw_symbol: str  # Original symbol token (#XAU, Gold, etc.)
    entry_min: Decimal | None = None
    entry_max: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    raw_tp_text: str | None = None  # e.g., "Trên 200pips"
    lot_size: Decimal | None = None


class ParsedManagementAction(BaseModel):
    """Structured management action on existing positions."""

    action_type: ManagementType
    percentage: Decimal | None = None  # e.g., 50 for 50% partial close
    target: str | None = None  # e.g., "ENTRY", "BREAKEVEN"
    price: Decimal | None = None
    keyword: str = ""  # The keyword that triggered this action


class ParsedSignal(BaseModel):
    """Unified container for all parsed signal outputs."""

    message_type: MessageType
    raw_message: str
    symbol: str | None = None
    new_order: ParsedNewOrder | None = None
    management_actions: list[ParsedManagementAction] = Field(default_factory=list)
    parser_version: str = "v1.0"
    metadata: dict[str, Any] = Field(default_factory=dict)
    is_reentry: bool = False
