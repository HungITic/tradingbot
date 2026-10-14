"""
Unit tests for Telegram replay feature and message link notification flow.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import ContextTypes

from app.broker.base import AccountInfo, OrderResult, TickInfo
from app.config.settings import settings
from app.telegram.personal_bot import PersonalTelegramBot
from app.telegram.signal_listener import TelegramSignalListener
from app.trading.risk import RiskConfig
from app.trading.service import TradingService


@pytest.fixture(autouse=True)
def mock_db_session():
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.rollback = AsyncMock()

    class MockAsyncContextManager:
        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

    with patch("app.config.database.get_session", return_value=MockAsyncContextManager()):
        yield mock_session


@pytest.fixture
def mock_broker():
    broker = MagicMock()
    broker.is_connected = AsyncMock(return_value=True)
    broker.get_account_info = AsyncMock(
        return_value=AccountInfo(
            login=123456,
            balance=Decimal("10000.00"),
            equity=Decimal("10000.00"),
            margin=Decimal("0"),
            free_margin=Decimal("10000.00"),
            leverage=100,
            server="DemoServer",
            currency="USD",
        )
    )
    from datetime import datetime, timezone

    broker.get_positions = AsyncMock(return_value=[])
    broker.get_history_deals = AsyncMock(return_value=[])
    broker.get_tick = AsyncMock(
        return_value=TickInfo(
            symbol="XAUUSD",
            bid=Decimal("2650.00"),
            ask=Decimal("2650.50"),
            time=datetime.now(timezone.utc),
        )
    )
    broker.open_order = AsyncMock(
        return_value=OrderResult(
            success=True,
            ticket=112233,
            volume=Decimal("0.01"),
            price=Decimal("2650.00"),
            comment="Executed",
        )
    )
    return broker


@pytest.mark.asyncio
async def test_notifications_include_message_link(mock_broker):
    """Test that all notifications emitted during signal processing include the original message link."""
    service = TradingService(broker=mock_broker)
    notifications = []

    async def mock_notify(msg: str, reply_markup=None):
        notifications.append(msg)

    service.set_notification_callback(mock_notify)

    raw_signal = (
        "Sell #XAU 2650 - 2652\n"
        "SL: 2660\n"
        "TP1: 2640\n"
        "TP2: 2630\n"
    )
    test_link = "https://t.me/MegaLodonGOLDForex/1241"

    await service.process_raw_message(
        raw_signal,
        message_id=1241,
        chat_id=-1001904907955,
        message_link=test_link,
    )

    # Verify notifications were sent
    assert len(notifications) >= 1
    for n in notifications:
        assert "🔗 <b>Signal:</b>" in n
        assert test_link in n


@pytest.mark.asyncio
async def test_slippage_warning_includes_message_link(mock_broker):
    """Test that slippage warnings include the original message link."""
    from datetime import datetime, timezone

    # Set market price far from entry (slippage)
    mock_broker.get_tick = AsyncMock(
        return_value=TickInfo(
            symbol="XAUUSD",
            bid=Decimal("2645.00"),  # 5 points slippage for SELL 2650
            ask=Decimal("2645.50"),
            time=datetime.now(timezone.utc),
        )
    )
    service = TradingService(broker=mock_broker)
    notifications = []

    async def mock_notify(msg: str, reply_markup=None):
        notifications.append(msg)

    service.set_notification_callback(mock_notify)

    raw_signal = "Sell #XAU 2650 - 2652\nSL: 2660\nTP: 2640"
    test_link = "https://t.me/MegaLodonGOLDForex/1241"

    await service.process_raw_message(
        raw_signal,
        message_id=1241,
        chat_id=-1001904907955,
        message_link=test_link,
    )

    assert len(notifications) == 1
    assert "Cảnh báo Min Entry: Vượt ngưỡng trượt giá" in notifications[0]
    assert "🔗 <b>Signal:</b>" in notifications[0]
    assert test_link in notifications[0]


@pytest.mark.asyncio
async def test_max_entry_exceeded_warning_includes_message_link(mock_broker):
    """Test that max entry exceeded warnings include the original message link."""
    from datetime import datetime, timezone

    # Set market price past max entry: for SELL 2650 - 2652 (where max_entry is 2652), price is 2655
    mock_broker.get_tick = AsyncMock(
        return_value=TickInfo(
            symbol="XAUUSD",
            bid=Decimal("2655.00"),
            ask=Decimal("2655.50"),
            time=datetime.now(timezone.utc),
        )
    )
    service = TradingService(broker=mock_broker)
    notifications = []

    async def mock_notify(msg: str, reply_markup=None):
        notifications.append(msg)

    service.set_notification_callback(mock_notify)

    raw_signal = "Sell #XAU 2650 - 2652\nSL: 2660\nTP: 2640"
    test_link = "https://t.me/MegaLodonGOLDForex/1241"

    await service.process_raw_message(
        raw_signal,
        message_id=1241,
        chat_id=-1001904907955,
        message_link=test_link,
    )

    assert len(notifications) == 1
    assert "Cảnh báo: Giá hiện tại vượt quá Max Entry" in notifications[0]
    assert "🔗 <b>Signal:</b>" in notifications[0]
    assert test_link in notifications[0]


@pytest.mark.asyncio
async def test_fetch_message_by_link_in_signal_listener():
    """Test signal_listener.fetch_message_by_link using a mocked Telethon client."""
    listener = TelegramSignalListener()
    mock_client = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=True)
    mock_client.is_user_authorized = AsyncMock(return_value=True)

    mock_msg = MagicMock()
    mock_msg.raw_text = "BUY GOLD 2650 SL 2640 TP 2660"
    mock_msg.id = 1241
    mock_msg.chat_id = -1001904907955
    mock_msg.is_reply = False
    mock_msg.reply_to_msg_id = None
    mock_msg.reply_to = None

    mock_chat = MagicMock()
    mock_chat.username = "MegaLodonGOLDForex"
    mock_msg.get_chat = AsyncMock(return_value=mock_chat)

    mock_client.get_messages = AsyncMock(return_value=mock_msg)
    listener.client = mock_client

    link = "https://t.me/MegaLodonGOLDForex/1241"
    raw_text, msg_id, chat_id, canonical_link, is_reentry, reply_to_msg_id = (
        await listener.fetch_message_by_link(link)
    )

    assert raw_text == "BUY GOLD 2650 SL 2640 TP 2660"
    assert msg_id == 1241
    assert chat_id == -1001904907955
    assert canonical_link == "https://t.me/MegaLodonGOLDForex/1241"
    assert is_reentry is False
    assert reply_to_msg_id is None


@pytest.mark.asyncio
async def test_cmd_replay_and_raw_link_handling():
    """Test /replay command and direct text paste in PersonalTelegramBot."""
    mock_service = MagicMock(spec=TradingService)
    mock_service.process_raw_message = AsyncMock()

    bot = PersonalTelegramBot(service=mock_service)

    # Mock admin update
    admin_id = settings.telegram.admin_chat_id or 996960102
    user = User(id=admin_id, first_name="Admin", is_bot=False)
    chat = Chat(id=admin_id, type="private")

    status_message = AsyncMock(spec=Message)
    status_message.edit_text = AsyncMock()

    incoming_message = AsyncMock(spec=Message)
    incoming_message.text = "/replay https://t.me/MegaLodonGOLDForex/1241"
    incoming_message.reply_text = AsyncMock(return_value=status_message)

    update = Update(update_id=1, message=incoming_message)
    update._effective_user = user
    update._effective_chat = chat

    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    context.args = ["https://t.me/MegaLodonGOLDForex/1241"]

    with patch(
        "app.telegram.signal_listener.signal_listener.fetch_message_by_link",
        new=AsyncMock(
            return_value=(
                "BUY GOLD 2650 SL 2640 TP 2660",
                1241,
                -1001904907955,
                "https://t.me/MegaLodonGOLDForex/1241",
                False,
                None,
            )
        ),
    ):
        await bot.cmd_replay(update, context)

        mock_service.process_raw_message.assert_awaited_once_with(
            "BUY GOLD 2650 SL 2640 TP 2660",
            is_reentry=False,
            message_id=1241,
            chat_id=-1001904907955,
            message_link="https://t.me/MegaLodonGOLDForex/1241",
            reply_to_msg_id=None,
        )
        assert status_message.edit_text.called

    # Test raw text paste directly without /replay
    mock_service.process_raw_message.reset_mock()
    incoming_message.text = "https://t.me/MegaLodonGOLDForex/1241"

    with patch(
        "app.telegram.signal_listener.signal_listener.fetch_message_by_link",
        new=AsyncMock(
            return_value=(
                "BUY GOLD 2650 SL 2640 TP 2660",
                1241,
                -1001904907955,
                "https://t.me/MegaLodonGOLDForex/1241",
                False,
                None,
            )
        ),
    ):
        await bot.handle_text_message(update, context)

        mock_service.process_raw_message.assert_awaited_once_with(
            "BUY GOLD 2650 SL 2640 TP 2660",
            is_reentry=False,
            message_id=1241,
            chat_id=-1001904907955,
            message_link="https://t.me/MegaLodonGOLDForex/1241",
            reply_to_msg_id=None,
        )
