"""Durable account-wide admission history for new signal executions."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class EntryAdmission(Base):
    __tablename__ = 'entry_admissions'
    __table_args__ = (
        UniqueConstraint('account_key', 'signal_id', name='uq_entry_admission_account_signal'),
        Index('ix_entry_admission_account_time', 'account_key', 'admitted_at'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_key: Mapped[str] = mapped_column(String(200), nullable=False)
    signal_id: Mapped[int] = mapped_column(ForeignKey('signals.id'), nullable=False)
    admitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    policy: Mapped[dict] = mapped_column(JSONB, nullable=False)
