"""
Daily aggregate statistics model for trading metrics and reporting.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import Date, Integer, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class DailyStatistic(Base, TimestampMixin):
    """
    Pre-computed daily summary statistics.
    """

    __tablename__ = "daily_statistics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[date] = mapped_column(Date, unique=True, nullable=False)

    total_signals: Mapped[int] = mapped_column(Integer, default=0)
    executed_signals: Mapped[int] = mapped_column(Integer, default=0)
    rejected_signals: Mapped[int] = mapped_column(Integer, default=0)

    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    winning_trades: Mapped[int] = mapped_column(Integer, default=0)
    losing_trades: Mapped[int] = mapped_column(Integer, default=0)

    total_profit: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"))
    total_loss: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"))
    net_pnl: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"))
    max_drawdown: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"))
