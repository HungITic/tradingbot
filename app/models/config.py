"""
Configuration and management rule models.

Provides dynamic runtime configuration stored in the database,
allowing Telegram admin console to adjust parameters without restarts.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class BotConfig(Base, TimestampMixin):
    """
    Dynamic configuration key-value storage.

    Categories: 'trading', 'risk', 'signal', 'management'
    """

    __tablename__ = "bot_config"
    __table_args__ = (
        UniqueConstraint("category", "key", name="uq_bot_config_category_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    description: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(50), nullable=True)


class ManagementRule(Base, TimestampMixin):
    """
    Mapping from signal keywords to execution actions.

    Example:
        keyword="chốt bớt", action_type="PARTIAL_CLOSE", parameters={"percentage": 50}
    """

    __tablename__ = "management_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    keyword: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    action_type: Mapped[str] = mapped_column(
        String(30), nullable=False
    )  # PARTIAL_CLOSE, CLOSE, MOVE_SL, BREAKEVEN
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
