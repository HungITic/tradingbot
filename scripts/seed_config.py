"""
Seed default configurations and initial management rules into PostgreSQL.

Run after running migrations:
    python scripts/seed_config.py
"""

import asyncio

import structlog
from sqlalchemy import select

from app.config.database import close_db, get_session, init_db
from app.models.config import BotConfig, ManagementRule

logger = structlog.get_logger(__name__)

DEFAULT_CONFIGS = [
    {
        "category": "trading",
        "key": "general",
        "value": {
            "enabled": True,
            "allowed_symbols": ["XAUUSD"],
            "trading_hours": "ALL",
        },
        "description": "General trading switches and symbols",
    },
    {
        "category": "risk",
        "key": "limits",
        "value": {
            "max_open_positions": 5,
            "max_lot": 0.05,
            "min_lot": 0.01,
            "default_lot": 0.01,
            "max_daily_loss_percent": 10.0,
            "require_sl": True,
            "require_tp": False,
        },
        "description": "Account-level risk parameters and guardrails",
    },
    {
        "category": "signal",
        "key": "timing",
        "value": {
            "duplicate_timeout_seconds": 600,
            "signal_expire_seconds": 300,
        },
        "description": "Signal freshness and deduplication parameters",
    },
]

DEFAULT_MANAGEMENT_RULES = [
    {
        "keyword": "chốt bớt",
        "action_type": "PARTIAL_CLOSE",
        "parameters": {"percentage": 50},
        "priority": 10,
    },
    {
        "keyword": "chốt hết",
        "action_type": "CLOSE",
        "parameters": {"percentage": 100},
        "priority": 10,
    },
    {
        "keyword": "chốt",
        "action_type": "CLOSE",
        "parameters": {"percentage": 100},
        "priority": 5,
    },
    {
        "keyword": "dời sl",
        "action_type": "MOVE_SL",
        "parameters": {"target": "ENTRY"},
        "priority": 8,
    },
    {
        "keyword": "dịch stoploss",
        "action_type": "MOVE_SL",
        "parameters": {"target": "ENTRY"},
        "priority": 8,
    },
    {
        "keyword": "dời stoploss",
        "action_type": "MOVE_SL",
        "parameters": {"target": "ENTRY"},
        "priority": 8,
    },
    {
        "keyword": "dừng",
        "action_type": "CLOSE",
        "parameters": {"percentage": 100},
        "priority": 1,
    },
]


async def seed() -> None:
    await init_db()

    async with get_session() as session:
        # 1. Seed configs
        for item in DEFAULT_CONFIGS:
            stmt = select(BotConfig).where(
                BotConfig.category == item["category"],
                BotConfig.key == item["key"],
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if not existing:
                cfg = BotConfig(
                    category=item["category"],
                    key=item["key"],
                    value=item["value"],
                    description=item["description"],
                    updated_by="SYSTEM_SEED",
                )
                session.add(cfg)
                logger.info("seeded_config", category=item["category"], key=item["key"])

        # 2. Seed management rules
        for rule in DEFAULT_MANAGEMENT_RULES:
            stmt = select(ManagementRule).where(
                ManagementRule.keyword == rule["keyword"]
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if not existing:
                r = ManagementRule(
                    keyword=rule["keyword"],
                    action_type=rule["action_type"],
                    parameters=rule["parameters"],
                    priority=rule["priority"],
                    enabled=True,
                )
                session.add(r)
                logger.info("seeded_management_rule", keyword=rule["keyword"])

    await close_db()
    print("✅ Seed completed successfully!")


if __name__ == "__main__":
    asyncio.run(seed())
