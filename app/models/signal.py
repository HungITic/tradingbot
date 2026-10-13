"""
Signal-related models — raw messages, parsed signals, and signal sources.

Audit trail:  raw Telegram message → parsed signal → risk evaluation → execution.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class SignalSource(Base, TimestampMixin):
    """Telegram groups / channels that produce trading signals."""

    __tablename__ = "signal_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Relationships
    signals: Mapped[list[Signal]] = relationship(back_populates="source")


class Signal(Base):
    """
    Raw signal received from a Telegram group.

    The ``raw_message`` is always preserved so we can replay through
    a newer parser or an AI classifier later.
    """

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("signal_sources.id"), nullable=True
    )
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Always keep the original text
    raw_message: Mapped[str] = mapped_column(Text, nullable=False)

    # Classification
    message_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # NEW_ORDER, CLOSE, PARTIAL_CLOSE, MOVE_SL, POSITION_MANAGEMENT, UNKNOWN

    # Processing status
    status: Mapped[str] = mapped_column(
        String(20), default="RECEIVED"
    )  # RECEIVED, PARSED, VALIDATED, EXECUTED, REJECTED, ERROR

    parser_version: Mapped[str | None] = mapped_column(String(20), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    source: Mapped[SignalSource | None] = relationship(back_populates="signals")
    parsed_signal: Mapped[ParsedSignal | None] = relationship(
        back_populates="signal", uselist=False
    )
    orders: Mapped[list] = relationship("Order", back_populates="signal")
    risk_events: Mapped[list] = relationship("RiskEvent", back_populates="signal")
    management_actions: Mapped[list] = relationship(
        "ManagementAction", back_populates="signal"
    )


class ParsedSignal(Base):
    """
    Structured data extracted from a raw signal message.

    Linked 1:1 to a ``Signal`` row.  If the parser is upgraded,
    a new ``ParsedSignal`` can be created for the same ``Signal``.
    """

    __tablename__ = "parsed_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("signals.id"), unique=True, nullable=False
    )

    action: Mapped[str | None] = mapped_column(String(10))  # BUY, SELL
    symbol: Mapped[str | None] = mapped_column(String(20))  # XAUUSD (normalized)
    entry_min: Mapped[Decimal | None] = mapped_column(Numeric(15, 5))
    entry_max: Mapped[Decimal | None] = mapped_column(Numeric(15, 5))
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(15, 5))
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(15, 5))
    take_profit_2: Mapped[Decimal | None] = mapped_column(Numeric(15, 5), nullable=True)
    raw_tp_text: Mapped[str | None] = mapped_column(
        String(100)
    )  # "Trên 200pips" — keep original
    lot_size: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))

    # Extra parsed data that doesn't fit fixed columns
    extra_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    signal: Mapped[Signal] = relationship(back_populates="parsed_signal")
