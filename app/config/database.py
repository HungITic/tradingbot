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
application_lock_pid: int | None = None


async def acquire_application_lock():
    """One active application writer per database; keep this transaction alive until shutdown."""
    global application_lock_pid
    from sqlalchemy import text
    connection = await engine.connect()
    try:
        acquired = await connection.scalar(text('SELECT pg_try_advisory_xact_lock(20261019, 1)'))
        if not acquired:
            raise RuntimeError('Another trading bot instance already holds the database execution lease')
        application_lock_pid = await connection.scalar(text('SELECT pg_backend_pid()'))
        return connection
    except BaseException:
        await connection.close()
        raise

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
        except BaseException:
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
            # Refuse startup on duplicate historical IDs; never silently delete financial records.
            await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_trades_mt5_ticket ON trades (mt5_ticket)"))
            await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_mt5_ticket ON orders (mt5_ticket)"))
            await conn.execute(text("ALTER TABLE signal_receipts ADD COLUMN IF NOT EXISTS raw_message TEXT"))
            await conn.execute(text("ALTER TABLE signal_receipts ADD COLUMN IF NOT EXISTS context JSONB"))
            await conn.execute(text("ALTER TABLE trades ADD COLUMN IF NOT EXISTS mt5_position_id BIGINT"))
            await conn.execute(text("ALTER TABLE trades ADD COLUMN IF NOT EXISTS mt5_order_ticket BIGINT"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_trades_mt5_position_id ON trades (mt5_position_id)"))
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
