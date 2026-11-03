"""Durable Telegram delivery queue, independent of the MT5 execution lock."""
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class TelegramNotification(Base, TimestampMixin):
    __tablename__ = 'telegram_notifications'
    __table_args__ = (
        CheckConstraint("status IN ('PENDING', 'SENDING', 'SENT', 'FAILED')", name='ck_telegram_notification_status'),
        Index('ix_telegram_notification_delivery', 'bot_key', 'chat_id', 'status', 'id'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_key: Mapped[str] = mapped_column(String(64), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    reply_markup: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default='PENDING')
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    claim_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
