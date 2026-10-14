"""Unit tests for batch config extraction and execution."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.telegram.personal_bot import PersonalTelegramBot
from app.trading.risk.models import RiskConfig
from app.trading.service import TradingService


def test_extract_config_pairs_multiline():
    raw_text = """/config set lot_min_entry 1
/config set lot_max_entry 1
/config set default_lot 1
/config set min_lot 1
/config set max_lot 5"""
    args = ["set", "lot_min_entry", "1"]
    pairs = PersonalTelegramBot._extract_config_pairs(raw_text, args)
    assert pairs == [
        ("lot_min_entry", "1"),
        ("lot_max_entry", "1"),
        ("default_lot", "1"),
        ("min_lot", "1"),
        ("max_lot", "5"),
    ]


def test_extract_config_pairs_without_slash():
    raw_text = """config set lot_min_entry 0.05
config set lot_max_entry 0.05"""
    pairs = PersonalTelegramBot._extract_config_pairs(raw_text, [])
    assert pairs == [
        ("lot_min_entry", "0.05"),
        ("lot_max_entry", "0.05"),
    ]


def test_extract_config_pairs_equal_signs():
    raw_text = "/config set lot_min_entry=2 lot_max_entry=3"
    args = ["set", "lot_min_entry=2", "lot_max_entry=3"]
    pairs = PersonalTelegramBot._extract_config_pairs(raw_text, args)
    assert pairs == [
        ("lot_min_entry", "2"),
        ("lot_max_entry", "3"),
    ]


def test_extract_config_pairs_single_standard():
    raw_text = "/config set default_lot 0.02"
    args = ["set", "default_lot", "0.02"]
    pairs = PersonalTelegramBot._extract_config_pairs(raw_text, args)
    assert pairs == [("default_lot", "0.02")]


@pytest.mark.asyncio
async def test_service_update_config_keys():
    mock_broker = AsyncMock()
    service = TradingService(broker=mock_broker)
    service.save_risk_config_to_db = AsyncMock(return_value=True)

    updates = {
        "lot_min_entry": "1",
        "lot_max_entry": "1",
        "default_lot": "1",
        "min_lot": "1",
        "max_lot": "5",
    }
    results = await service.update_config_keys(updates)
    assert all(results.values())
    assert service.risk_config.lot_min_entry == Decimal("1")
    assert service.risk_config.lot_max_entry == Decimal("1")
    assert service.risk_config.default_lot == Decimal("1")
    assert service.risk_config.min_lot == Decimal("1")
    assert service.risk_config.max_lot == Decimal("5")
    service.save_risk_config_to_db.assert_awaited_once()


@pytest.mark.asyncio
async def test_cmd_config_batch_execution():
    mock_broker = AsyncMock()
    service = TradingService(broker=mock_broker)
    service.save_risk_config_to_db = AsyncMock(return_value=True)

    bot = PersonalTelegramBot(service=service)

    update = MagicMock()
    message = AsyncMock()
    raw_text = """/config set lot_min_entry 1
/config set lot_max_entry 1
/config set default_lot 1
/config set min_lot 1
/config set max_lot 5"""
    message.text = raw_text
    update.effective_message = message
    update.message = message
    update.effective_user.id = 123456

    context = MagicMock()
    context.args = ["set", "lot_min_entry", "1"]

    # Bypass admin check
    await bot.cmd_config.__wrapped__(bot, update, context)

    # Check response message
    message.reply_text.assert_awaited_once()
    reply_call = message.reply_text.call_args[0][0]
    assert "Đã cập nhật 5 cấu hình vào DB" in reply_call
    assert "lot_min_entry" in reply_call
    assert "lot_max_entry" in reply_call
    assert "default_lot" in reply_call
    assert "min_lot" in reply_call
    assert "max_lot" in reply_call
