"""
Trade-related models — orders, positions, trades, and management actions.

Full audit trail: signal → order → position → trades.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.signal import Signal


class Order(Base):
    """
    An order sent to MT5.

    Each order is linked back to the signal that triggered it.
    """

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("signals.id"), nullable=True
    )
    mt5_ticket: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mt5_order_type: Mapped[int | None] = mapped_column(Integer, nullable=True)

    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    volume: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    price: Mapped[Decimal | None] = mapped_column(Numeric(15, 5))
    sl: Mapped[Decimal | None] = mapped_column(Numeric(15, 5))
    tp: Mapped[Decimal | None] = mapped_column(Numeric(15, 5))
    deviation: Mapped[int | None] = mapped_column(Integer)
    magic: Mapped[int | None] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(String(200))

    # Execution result
    status: Mapped[str | None] = mapped_column(
        String(20)
    )  # PENDING, FILLED, REJECTED, CANCELLED, ERROR
    mt5_retcode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mt5_comment: Mapped[str | None] = mapped_column(String(200), nullable=True)

    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    signal: Mapped["Signal | None"] = relationship("Signal", back_populates="orders")
    position: Mapped["Position | None"] = relationship(
        back_populates="order", uselist=False
    )


class Position(Base, TimestampMixin):
    """
    An open or closed trading position.

    Tracks current state and links back to the originating order.
    """

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("orders.id"), nullable=True
    )
    mt5_ticket: Mapped[int | None] = mapped_column(
        BigInteger, unique=True, nullable=True
    )

    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    volume: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    open_price: Mapped[Decimal | None] = mapped_column(Numeric(15, 5))
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(15, 5))
    sl: Mapped[Decimal | None] = mapped_column(Numeric(15, 5))
    tp: Mapped[Decimal | None] = mapped_column(Numeric(15, 5))
    profit: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    swap: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    commission: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))

    position_type: Mapped[str | None] = mapped_column(String(10))  # BUY, SELL
    status: Mapped[str | None] = mapped_column(
        String(20)
    )  # OPEN, CLOSED, PARTIALLY_CLOSED
    opened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    close_reason: Mapped[str | None] = mapped_column(
        String(50)
    )  # SIGNAL, MANUAL, SL, TP, MANAGEMENT

    # Relationships
    order: Mapped[Order | None] = relationship(back_populates="position")
    trades: Mapped[list[Trade]] = relationship(back_populates="position")
    management_actions: Mapped[list] = relationship(
        "ManagementAction", back_populates="position"
    )


class Trade(Base):
    """
    An individual deal / execution within a position.

    A position may have multiple trades (partial closes, etc.).
    """

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("positions.id"), nullable=True
    )
    mt5_ticket: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mt5_deal_type: Mapped[int | None] = mapped_column(Integer, nullable=True)

    symbol: Mapped[str | None] = mapped_column(String(20))
    volume: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    price: Mapped[Decimal | None] = mapped_column(Numeric(15, 5))
    profit: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    swap: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    commission: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))

    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    position: Mapped[Position | None] = relationship(back_populates="trades")


class ManagementAction(Base):
    """
    A management action applied to a position (partial close, move SL, etc.).

    Triggered either by a signal message or by a Telegram command.
    """

    __tablename__ = "management_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("signals.id"), nullable=True
    )
    position_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("positions.id"), nullable=True
    )

    action_type: Mapped[str | None] = mapped_column(
        String(30)
    )  # PARTIAL_CLOSE, MOVE_SL, CLOSE, BREAKEVEN
    parameters: Mapped[dict | None] = mapped_column(JSONB)  # {"percentage": 50}
    status: Mapped[str | None] = mapped_column(
        String(20)
    )  # PENDING, EXECUTED, FAILED
    result: Mapped[dict | None] = mapped_column(JSONB)

    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    signal: Mapped["Signal | None"] = relationship(
        "Signal", back_populates="management_actions"
    )
    position: Mapped[Position | None] = relationship(
        back_populates="management_actions"
    )
