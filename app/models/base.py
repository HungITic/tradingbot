"""
SQLAlchemy declarative base with common mixins.

Every model inherits from ``Base`` and automatically gets
``id``, ``created_at``, and ``updated_at`` columns.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all models."""

    pass


class TimestampMixin:
    """Adds ``created_at`` / ``updated_at`` to any model."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
