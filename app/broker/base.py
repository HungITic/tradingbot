"""
Broker abstraction layer.

Decouples trading logic, risk engine, and Telegram bot from specific broker APIs.
MT5, Binance, cTrader, or custom REST bridges all implement `BrokerAdapter`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass
class AccountInfo:
    """Standardized broker account snapshot."""

    login: int
    balance: Decimal
    equity: Decimal
    margin: Decimal
    free_margin: Decimal
    leverage: int
    server: str
    currency: str
    trade_mode: str = "DEMO"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class TickInfo:
    """Current price tick for a symbol."""

    symbol: str
    bid: Decimal
    ask: Decimal
    time: datetime
    spread_points: Decimal = Decimal("0")


@dataclass
class SymbolInfo:
    """Symbol specifications and trading constraints."""

    symbol: str
    digits: int
    point: Decimal
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    trade_contract_size: Decimal


@dataclass
class OrderRequest:
    """Request to open a new market or pending order."""

    symbol: str
    order_type: str  # "BUY", "SELL"
    volume: Decimal
    price: Decimal | None = None
    sl: Decimal | None = None
    tp: Decimal | None = None
    tp1: Decimal | None = None
    tp2: Decimal | None = None
    deviation: int = 20
    magic: int = 234000
    comment: str = ""
    signal_id: int | None = None


@dataclass
class OrderResult:
    """Outcome of an order execution request."""

    success: bool
    ticket: int | None = None
    retcode: int = 0
    price: Decimal | None = None
    volume: Decimal | None = None
    comment: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class PositionInfo:
    """Active position details."""

    ticket: int
    symbol: str
    volume: Decimal
    open_price: Decimal
    current_price: Decimal
    sl: Decimal | None
    tp: Decimal | None
    profit: Decimal
    swap: Decimal
    commission: Decimal
    position_type: str  # "BUY", "SELL"
    open_time: datetime
    magic: int = 0
    comment: str = ""
    identifier: int = 0  # Stable POSITION_IDENTIFIER, which can differ from ticket.
    members: tuple[PositionInfo, ...] = ()  # Logical entry branch; broker objects stay individual.
    initial_volume: Decimal | None = None


@dataclass
class DealInfo:
    """Historical executed deal record (entry or exit)."""

    ticket: int
    order: int
    position_id: int
    symbol: str
    deal_type: str  # "BUY", "SELL", "BALANCE", etc.
    entry: str  # "IN", "OUT", "INOUT", "OUT_BY"
    volume: Decimal
    price: Decimal
    profit: Decimal
    swap: Decimal
    commission: Decimal
    time: datetime
    reason: int = 0
    magic: int = 0
    comment: str = ""


@dataclass
class PartialCloseDealInfo:
    """Individual partial exit deal within a position."""

    ticket: int
    volume: Decimal
    price: Decimal
    profit: Decimal
    time: datetime


@dataclass
class ClosedPositionInfo:
    """Aggregated historical closed or partially closed position record."""

    ticket: int  # position_id / position ticket
    symbol: str
    position_type: str  # "BUY" or "SELL"
    volume: Decimal  # Total closed volume
    open_price: Decimal | None
    close_price: Decimal  # Volume-weighted average exit price
    profit: Decimal  # Total realized gross profit
    swap: Decimal
    commission: Decimal
    net_pnl: Decimal
    open_time: datetime | None
    close_time: datetime
    deals_count: int = 1
    is_fully_closed: bool | None = True
    total_volume: Decimal | None = None  # Initial / total volume
    currency: str = "USD"
    partial_deals: list[PartialCloseDealInfo] = field(default_factory=list)
    comment: str = ""
    opening_order_tickets: tuple[int, ...] = ()

    def __post_init__(self):
        if self.total_volume is None or self.total_volume <= Decimal("0"):
            self.total_volume = self.volume


@dataclass
class PendingOrderInfo:
    """Active pending order (Limit/Stop)."""
    ticket: int
    symbol: str
    order_type: str  # "BUY_LIMIT", "SELL_STOP", etc.
    volume: Decimal
    price: Decimal
    sl: Decimal | None
    tp: Decimal | None
    comment: str
    setup_time: datetime


class BrokerAdapter(ABC):
    """Abstract interface that all broker implementations must satisfy."""

    @abstractmethod
    async def connect(self) -> bool:
        """Establish connection to terminal or broker gateway."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully release broker connection."""
        ...

    @abstractmethod
    async def is_connected(self) -> bool:
        """Check if connection is alive and trading is permitted."""
        ...

    @abstractmethod
    async def get_account_info(self) -> AccountInfo:
        """Retrieve live balance, equity, and margin information."""
        ...

    @abstractmethod
    async def get_tick(self, symbol: str) -> TickInfo:
        """Fetch current bid/ask tick for symbol."""
        ...

    @abstractmethod
    async def get_symbol_info(self, symbol: str) -> SymbolInfo:
        """Fetch symbol specifications (tick size, min lot, digits)."""
        ...

    @abstractmethod
    async def get_positions(self, symbol: str | None = None) -> list[PositionInfo]:
        """Fetch open positions, optionally filtered by symbol."""
        ...

    @abstractmethod
    async def get_pending_orders(self, symbol: str | None = None) -> list[PendingOrderInfo]:
        """Fetch pending orders, optionally filtered by symbol."""
        ...

    async def resolve_symbol(self, symbol: str) -> str:
        """Resolve broker-specific symbol name (e.g. XAUUSD -> XAUUSDm). Default returns input symbol."""
        return symbol

    @abstractmethod
    async def open_order(self, request: OrderRequest) -> OrderResult:
        """Execute a market order or place a pending order."""
        ...

    @abstractmethod
    async def cancel_order(self, ticket: int) -> OrderResult:
        """Cancel a pending order by ticket."""
        ...

    @abstractmethod
    async def modify_position(
        self,
        ticket: int,
        sl: Decimal | None = None,
        tp: Decimal | None = None,
    ) -> OrderResult:
        """Adjust Stop Loss and/or Take Profit on an open position."""
        ...

    @abstractmethod
    async def close_position(
        self,
        ticket: int,
        volume: Decimal | None = None,
    ) -> OrderResult:
        """Close an open position fully or partially."""
        ...

    @abstractmethod
    async def get_history_deals(
        self, days: int = 7, symbol: str | None = None, start_time: datetime | None = None,
        position_id: int | None = None,
    ) -> list[DealInfo]:
        """Fetch deals by date range, or all deals for an exact broker position_id."""
        ...

