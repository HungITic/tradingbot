"""
Seed default configurations and initial management rules into PostgreSQL.

Run after running migrations:
    python scripts/seed_config.py
"""

import asyncio
import os
import sys

_root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root_dir not in sys.path:
    sys.path.insert(0, _root_dir)

import structlog

from app.config.database import close_db, init_db
from app.config.seeder import seed_initial_data

logger = structlog.get_logger(__name__)


async def seed() -> None:
    await init_db()
    results = await seed_initial_data()
    await close_db()
    print(f"✅ Seed completed successfully! Configs updated: {results['configs_seeded']}, Rules updated: {results['rules_seeded']}")


if __name__ == "__main__":
    asyncio.run(seed())
