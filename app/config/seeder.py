"""
Database configuration and rule seeder.

Populates initial configuration in `bot_config` and all 38 management rules
in `management_rules` table, ensuring PostgreSQL is the single source of truth.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_session
from app.models.config import BotConfig, ManagementRule
from app.signal.rules import DEFAULT_RULES

logger = structlog.get_logger(__name__)

INITIAL_BOT_CONFIGS: list[dict[str, Any]] = [
    {
        "category": "trading",
        "key": "general",
        "value": {
            "enabled": True,
            "allowed_symbols": ["XAUUSD", "XAUUSDm", "XAUUSD.s", "XAUUSDc", "XAUUSD.c", "XAUUSD.crp"],
            "trading_hours": "ALL",
        },
        "description": "General trading switches and allowed symbols",
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
        "category": "risk",
        "key": "dual_entry",
        "value": {
            "enable_dual_entry": True,
            "lot_min_entry": 0.01,
            "lot_max_entry": 0.01,
            "max_slippage_entry": 1.0,
        },
        "description": "Dual entry setup parameters (limit & market orders)",
    },
    {
        "category": "risk",
        "key": "position_management",
        "value": {
            "default_partial_close_percent": 50.0,
            "auto_breakeven_on_partial": True,
            "breakeven_offset_price": 0.0,
            "breakeven_mode": "ticket",
            "breakeven_only_improve": True,
            "require_explicit_close": True,
            "generic_partial_uses_default": True,
            "tp1_enabled": True,
            "tp1_min_volume_action": "breakeven",
            "auto_cancel_pending_on_full_close": True,
        },
        "description": "Position management and TP milestone automation",
    },
    {
        "category": "signal",
        "key": "timing",
        "value": {
            "duplicate_timeout_seconds": 10,
            "signal_expire_seconds": 300,
            "broker_utc_offset_hours": 3,
        },
        "description": "Signal freshness, broker timezone offset, and deduplication parameters",
    },
]


def _serialize_params(params: dict[str, Any]) -> dict[str, Any]:
    """Convert Decimal values to float/int for JSONB serialization."""
    serialized = {}
    for k, v in params.items():
        if isinstance(v, Decimal):
            serialized[k] = float(v)
        else:
            serialized[k] = v
    return serialized


async def seed_initial_data(session: AsyncSession | None = None) -> dict[str, int]:
    """
    Seed initial configuration and all management rules into PostgreSQL.

    Ensures:
    1. All 5 configuration sections exist in `bot_config` table.
       Missing keys in existing entries are merged cleanly.
    2. All 38 Vietnamese management rules from `DEFAULT_RULES` exist
       and are enabled in `management_rules` table.
    """
    if session is not None:
        return await _seed_with_session(session)

    async with get_session() as s:
        return await _seed_with_session(s)


async def _seed_with_session(session: AsyncSession) -> dict[str, int]:
    configs_seeded = 0
    rules_seeded = 0

    # 1. Seed / merge BotConfig entries
    for item in INITIAL_BOT_CONFIGS:
        stmt = select(BotConfig).where(
            BotConfig.category == item["category"],
            BotConfig.key == item["key"],
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()

        if existing is None:
            cfg = BotConfig(
                category=item["category"],
                key=item["key"],
                value=item["value"],
                description=item["description"],
                updated_by="SYSTEM_SEED",
            )
            session.add(cfg)
            configs_seeded += 1
            logger.info("seeded_bot_config", category=item["category"], key=item["key"])
        else:
            # Merge any missing keys from default into existing value
            val = dict(existing.value or {})
            changed = False
            for k, v in item["value"].items():
                if k not in val:
                    val[k] = v
                    changed = True
                elif k == "breakeven_mode" and val[k] != "ticket":
                    val[k] = "ticket"
                    changed = True
                elif k == "allowed_symbols" and isinstance(val[k], list) and isinstance(v, list):
                    # Ensure all default allowed symbols are included
                    merged_syms = list(dict.fromkeys(val[k] + v))
                    if merged_syms != val[k]:
                        val[k] = merged_syms
                        changed = True

            if changed:
                existing.value = val
                configs_seeded += 1
                logger.info("updated_bot_config_defaults", category=item["category"], key=item["key"])

    # 2. Seed all 38 DEFAULT_RULES into management_rules
    for rule in DEFAULT_RULES:
        stmt = select(ManagementRule).where(
            ManagementRule.keyword == rule.keyword
        )
        existing_rule = (await session.execute(stmt)).scalar_one_or_none()
        db_params = _serialize_params(rule.default_params)

        if existing_rule is None:
            r = ManagementRule(
                keyword=rule.keyword,
                action_type=rule.action_type.value,
                parameters=db_params,
                priority=rule.priority,
                enabled=True,
            )
            session.add(r)
            rules_seeded += 1
            logger.info("seeded_management_rule", keyword=rule.keyword, action=rule.action_type.value)
        # Existing rules, including disabled rules, are user configuration.
        # Startup must not silently re-enable or overwrite them.

    await session.flush()
    logger.info("seed_completed", configs_updated=configs_seeded, rules_updated=rules_seeded)
    return {"configs_seeded": configs_seeded, "rules_seeded": rules_seeded}
