"""
Risk evaluation and system event models for full audit trail.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.signal import Signal


class RiskEvent(Base):
    """
    Audit log of every risk check performed on a signal or action.
    """

    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("signals.id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # APPROVED, REJECTED, WARNING
    rule_name: Mapped[str] = mapped_column(
        String(100), nullable=False
    )  # max_positions, max_lot, daily_loss, etc.
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    signal: Mapped["Signal | None"] = relationship(  # noqa: F821
        "Signal", back_populates="risk_events"
    )


class BotEvent(Base):
    """
    Audit log for bot lifecycle events, admin operations, and critical errors.
    """

    __tablename__ = "bot_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # START, STOP, PAUSE, RESUME, ERROR, CONFIG_CHANGE, EMERGENCY_STOP
    source: Mapped[str] = mapped_column(
        String(50), default="SYSTEM"
    )  # SYSTEM, TELEGRAM, SIGNAL
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
