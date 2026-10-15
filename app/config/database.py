"""
Async SQLAlchemy engine & session factory.

Usage::

    from app.config.database import get_session

    async with get_session() as session:
        result = await session.execute(select(Signal))
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

import structlog
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config.settings import settings

logger = structlog.get_logger(__name__)

# Engine — single instance for the app lifetime
engine = create_async_engine(
    settings.db.url,
    echo=settings.debug,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
)

# Session factory
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional database session scope."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Verify database connectivity on startup, auto-create missing tables, and ensure schema migrations."""
    from sqlalchemy import text

    from app.models import Base  # imports all models into Base.metadata

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Ensure recently added columns exist (idempotent for PostgreSQL)
        if engine.dialect.name == "postgresql":
            await conn.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS tp1 NUMERIC(15, 5);"))
            await conn.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS tp2 NUMERIC(15, 5);"))
            await conn.execute(text("ALTER TABLE parsed_signals ADD COLUMN IF NOT EXISTS take_profit_2 NUMERIC(15, 5);"))
            await conn.execute(text("""INSERT INTO signal_receipts (chat_id, message_id)
SELECT DISTINCT src.chat_id, sig.telegram_message_id
FROM signals sig JOIN signal_sources src ON src.id = sig.source_id
JOIN orders ord ON ord.signal_id = sig.id
WHERE sig.telegram_message_id IS NOT NULL AND ord.mt5_ticket IS NOT NULL
ON CONFLICT DO NOTHING"""))
    logger.info("database_connected", url=settings.db.url.split("@")[-1])


async def close_db() -> None:
    """Dispose the engine pool on shutdown."""
    await engine.dispose()
    logger.info("database_disconnected")
