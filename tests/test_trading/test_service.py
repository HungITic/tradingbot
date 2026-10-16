"""
Unit tests for TradingService (end-to-end signal processing, risk, broker routing).
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.broker.base import (
    AccountInfo,
    BrokerAdapter,
    DealInfo,
    OrderRequest,
    OrderResult,
    PositionInfo,
    TickInfo,
)
from app.trading.risk import RiskConfig, RiskEngine
from app.trading.service import TradingService


@pytest.fixture(autouse=True)
def mock_db_session():
    mock_session = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.execute.return_value = MagicMock()
    mock_session.execute.return_value.scalar_one_or_none.return_value = 1
    mock_session.scalars.return_value = MagicMock()
    mock_session.scalars.return_value.all.return_value = []
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
    broker = MagicMock(spec=BrokerAdapter)
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
            ticket=999888,
            volume=Decimal("0.01"),
            price=Decimal("2650.00"),
            comment="Executed",
        )
    )
    broker.close_position = AsyncMock(
        return_value=OrderResult(success=True, ticket=999888, comment="Closed")
    )
    broker.modify_position = AsyncMock(
        return_value=OrderResult(success=True, ticket=999888, comment="Modified")
    )
    from app.broker.base import SymbolInfo
    broker.get_symbol_info = AsyncMock(return_value=SymbolInfo("XAUUSD", 5, Decimal(".00001"), Decimal(".01"), Decimal("100"), Decimal(".01"), Decimal("100")))
    return broker


@pytest.mark.asyncio
async def test_service_executes_new_order_signal(mock_broker):
    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    notifications = []

    async def mock_notify(msg: str):
        notifications.append(msg)

    service.set_notification_callback(mock_notify)

    raw_signal = (
        "Sell #XAU 2650 - 2652\n"
        "SL: 2660\n"
        "TP: Trên 200pips"
    )

    await service.process_raw_message(raw_signal)

    # Broker open_order called twice (Min Entry + Max Entry)
    assert mock_broker.open_order.call_count == 2
    call_req_1 = mock_broker.open_order.call_args_list[0][0][0]
    assert call_req_1.symbol in ("XAUUSD", "XAUUSDm")
    assert call_req_1.order_type == "SELL"
    assert call_req_1.volume == Decimal("0.01")
    assert call_req_1.sl == Decimal("2660")

    call_req_2 = mock_broker.open_order.call_args_list[1][0][0]
    assert call_req_2.symbol in ("XAUUSD", "XAUUSDm")
    assert call_req_2.order_type == "SELL_LIMIT"
    assert call_req_2.volume == Decimal("0.01")
    assert call_req_2.price == Decimal("2652")
    assert call_req_2.sl == Decimal("2660")

    # Notifications sent for both orders
    assert len(notifications) == 2
    assert "Order Executed (Min Entry)" in notifications[0]
    assert "Order Placed (Max Entry)" in notifications[1]


@pytest.mark.asyncio
async def test_service_single_price_signal_executes_one_order(mock_broker):
    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    notifications = []

    async def mock_notify(msg: str):
        notifications.append(msg)

    service.set_notification_callback(mock_notify)

    raw_signal = (
        "BUY XAUUSD 2650.0\n"
        "SL: 2640\n"
        "TP: 2670"
    )

    await service.process_raw_message(raw_signal)

    assert mock_broker.open_order.call_count == 1
    call_req = mock_broker.open_order.call_args[0][0]
    assert call_req.order_type == "BUY"
    assert len(notifications) == 1
    assert "Order Executed" in notifications[0]


@pytest.mark.asyncio
async def test_service_favorable_price_executes_market_order(mock_broker):
    from datetime import datetime, timezone
    # SELL 4148, current price is 4149 (favorable)
    mock_broker.get_tick = AsyncMock(
        return_value=TickInfo(
            symbol="XAUUSDm",
            bid=Decimal("4149.0"),
            ask=Decimal("4149.5"),
            time=datetime.now(timezone.utc),
        )
    )
    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    notifications = []
    service.set_notification_callback(lambda msg: notifications.append(msg))

    raw_signal = "Sell #XAU 4148 - 4160\nSL: 4170\nTP: Trên 200pips"
    await service.process_raw_message(raw_signal)

    # Order 1 (Min Entry at favorable current price 4149) should be executed
    assert mock_broker.open_order.call_count == 2
    call_req_1 = mock_broker.open_order.call_args_list[0][0][0]
    assert call_req_1.order_type == "SELL"
    assert call_req_1.price == Decimal("4149.0")


@pytest.mark.asyncio
async def test_service_acceptable_slippage_executes_market_order(mock_broker):
    from datetime import datetime, timezone
    # SELL 4148, current price is 4147 (unfavorable by 1.0 <= max_slippage 1.0)
    mock_broker.get_tick = AsyncMock(
        return_value=TickInfo(
            symbol="XAUUSDm",
            bid=Decimal("4147.0"),
            ask=Decimal("4147.5"),
            time=datetime.now(timezone.utc),
        )
    )
    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    notifications = []
    service.set_notification_callback(lambda msg: notifications.append(msg))

    raw_signal = "Sell #XAU 4148 - 4160\nSL: 4170\nTP: Trên 200pips"
    await service.process_raw_message(raw_signal)

    # Order 1 accepted because slippage is 1.0 <= 1.0
    assert mock_broker.open_order.call_count == 2
    call_req_1 = mock_broker.open_order.call_args_list[0][0][0]
    assert call_req_1.order_type == "SELL"
    assert call_req_1.price == Decimal("4147.0")


@pytest.mark.asyncio
async def test_service_excessive_slippage_skips_market_order_and_warns(mock_broker):
    from datetime import datetime, timezone
    # SELL 4148, current price is 4146.5 (unfavorable by 1.5 > max_slippage 1.0)
    mock_broker.get_tick = AsyncMock(
        return_value=TickInfo(
            symbol="XAUUSDm",
            bid=Decimal("4146.5"),
            ask=Decimal("4147.0"),
            time=datetime.now(timezone.utc),
        )
    )
    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    notifications = []
    service.set_notification_callback(lambda msg: notifications.append(msg))

    raw_signal = "Sell #XAU 4148 - 4160\nSL: 4170\nTP: Trên 200pips"
    await service.process_raw_message(raw_signal)

    # When excessive slippage, execution is paused for user decision (0 orders placed automatically)
    assert mock_broker.open_order.call_count == 0

    # Warning alert must be in notifications
    assert any("Cảnh báo Min Entry: Vượt ngưỡng trượt giá" in n for n in notifications)


@pytest.mark.asyncio
async def test_service_middle_zone_executes_both_market_and_pending_limit(mock_broker):
    from datetime import datetime, timezone
    # SELL 4148 - 4160, current price is 4154 (midpoint)
    mock_broker.get_tick = AsyncMock(
        return_value=TickInfo(
            symbol="XAUUSDm",
            bid=Decimal("4154.0"),
            ask=Decimal("4154.5"),
            time=datetime.now(timezone.utc),
        )
    )
    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    notifications = []
    service.set_notification_callback(lambda msg: notifications.append(msg))

    raw_signal = "Sell #XAU 4148 - 4160\nSL: 4170\nTP: Trên 200pips"
    await service.process_raw_message(raw_signal)

    # Both market order (Min Entry) and pending limit (Max Entry) should be placed
    assert mock_broker.open_order.call_count == 2
    req_1 = mock_broker.open_order.call_args_list[0][0][0]
    assert req_1.order_type == "SELL"
    assert req_1.price == Decimal("4154.0")
    assert req_1.comment == "bot-min-entry"

    req_2 = mock_broker.open_order.call_args_list[1][0][0]
    assert req_2.order_type == "SELL_LIMIT"
    assert req_2.price == Decimal("4160")
    assert req_2.comment == "bot-max-entry"

    assert len(notifications) == 2
    assert "Order Executed (Min Entry)" in notifications[0]
    assert "Order Placed (Max Entry)" in notifications[1]


@pytest.mark.asyncio
async def test_service_price_exceeding_max_entry_warns_and_skips(mock_broker):
    from datetime import datetime, timezone
    # SELL 4148 - 4160, current price is 4162.0 (exceeded max entry 4160)
    mock_broker.get_tick = AsyncMock(
        return_value=TickInfo(
            symbol="XAUUSDm",
            bid=Decimal("4162.0"),
            ask=Decimal("4162.5"),
            time=datetime.now(timezone.utc),
        )
    )
    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    notifications = []
    service.set_notification_callback(lambda msg: notifications.append(msg))

    raw_signal = "Sell #XAU 4148 - 4160\nSL: 4170\nTP: Trên 200pips"
    await service.process_raw_message(raw_signal)

    # No order should be placed at all!
    assert mock_broker.open_order.call_count == 0
    assert any("Giá hiện tại vượt quá Max Entry" in n for n in notifications)


@pytest.mark.asyncio
async def test_service_rejects_signal_when_paused(mock_broker):
    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    service.risk_config.enabled = False
    notifications = []

    async def mock_notify(msg: str):
        notifications.append(msg)

    service.set_notification_callback(mock_notify)

    raw_signal = (
        "Buy #XAU 2650 - 2652\n"
        "SL: 2640\n"
        "TP: 2670"
    )

    await service.process_raw_message(raw_signal)

    assert not mock_broker.open_order.called
    assert len(notifications) == 1
    assert "Signal Rejected" in notifications[0]
    assert "paused or disabled" in notifications[0]


@pytest.mark.asyncio
async def test_service_handles_partial_close_and_auto_breakeven(mock_broker):
    from datetime import datetime, timezone

    # Simulate open position
    pos = PositionInfo(
        ticket=112233,
        symbol="XAUUSDm",
        volume=Decimal("0.02"),
        open_price=Decimal("2650.00"),
        current_price=Decimal("2640.00"),
        sl=Decimal("2660.00"),
        tp=Decimal("2600.00"),
        profit=Decimal("20.00"),
        swap=Decimal("0"),
        commission=Decimal("0"),
        position_type="SELL",
        open_time=datetime.now(timezone.utc),
    )
    mock_broker.get_positions = AsyncMock(return_value=[pos])

    async def management_tick(symbol):
        from datetime import datetime, timezone
        positions = mock_broker.get_positions.return_value
        matching = next((p for p in positions if p.symbol == symbol), positions[0])
        return TickInfo(symbol, matching.current_price, matching.current_price + Decimal(".01"), datetime.now(timezone.utc))
    mock_broker.get_tick.side_effect = management_tick

    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    service.risk_config.auto_breakeven_on_partial = True
    service.risk_config.default_partial_close_percent = Decimal("50.0")

    notifications = []

    async def mock_notify(msg: str):
        notifications.append(msg)

    service.set_notification_callback(mock_notify)

    # Signal: "Chốt bớt" (no explicit %, defaults to 50% -> 0.01 lot)
    raw_signal = "Chốt bớt vàng"
    await service.process_raw_message(raw_signal)

    # 1. Partial close called
    assert mock_broker.close_position.called
    close_call = mock_broker.close_position.call_args[0]
    assert close_call[0] == 112233
    assert close_call[1] == Decimal("0.01")  # 50% of 0.02

    # 2. Auto Breakeven triggered
    assert mock_broker.modify_position.called
    modify_kwargs = mock_broker.modify_position.call_args
    assert modify_kwargs.kwargs["sl"] == Decimal("2650.00")  # entry price

    # 3. Notifications sent for both close and auto BE
    assert len(notifications) == 2
    assert "Position Management: Chốt 50%" in notifications[0]
    assert "Auto Breakeven on Partial" in notifications[1]


@pytest.mark.asyncio
async def test_service_skips_duplicate_partial_close_after_tp1(mock_broker):
    from datetime import datetime, timezone

    pos = PositionInfo(
        ticket=999888,
        symbol="XAUUSDm",
        volume=Decimal("0.02"),
        open_price=Decimal("2650.00"),
        current_price=Decimal("2660.00"),
        sl=Decimal("2650.00"),
        tp=Decimal("2680.00"),
        profit=Decimal("20.00"),
        swap=Decimal("0"),
        commission=Decimal("0"),
        position_type="BUY",
        open_time=datetime.now(timezone.utc),
    )
    mock_broker.get_positions = AsyncMock(return_value=[pos])

    async def management_tick(symbol):
        from datetime import datetime, timezone
        positions = mock_broker.get_positions.return_value
        matching = next((p for p in positions if p.symbol == symbol), positions[0])
        return TickInfo(symbol, matching.current_price, matching.current_price + Decimal(".01"), datetime.now(timezone.utc))
    mock_broker.get_tick.side_effect = management_tick

    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    # Simulate that TP1 was already executed by cronjob
    service._tp1_processed_tickets.add(999888)

    notifications = []

    async def mock_notify(msg: str):
        notifications.append(msg)

    service.set_notification_callback(mock_notify)

    # Signal: Channel says "chốt bớt vàng"
    await service.process_raw_message("Kèo vàng TP1 rồi chốt bớt 30% dời SL nha")

    # close_position should NOT be called because it was already partially closed!
    assert not mock_broker.close_position.called

    # Notification sent explaining skip
    assert any("Bỏ qua chốt bớt" in n for n in notifications)
    assert any("Vị thế đã chốt 1 phần trước đó" in n for n in notifications)


@pytest.mark.asyncio
async def test_service_allows_multiple_partial_closes_when_configured(mock_broker):
    from datetime import datetime, timezone

    pos = PositionInfo(
        ticket=999889,
        symbol="XAUUSDm",
        volume=Decimal("0.02"),
        open_price=Decimal("2650.00"),
        current_price=Decimal("2660.00"),
        sl=Decimal("2650.00"),
        tp=Decimal("2680.00"),
        profit=Decimal("20.00"),
        swap=Decimal("0"),
        commission=Decimal("0"),
        position_type="BUY",
        open_time=datetime.now(timezone.utc),
    )
    mock_broker.get_positions = AsyncMock(return_value=[pos])

    async def management_tick(symbol):
        from datetime import datetime, timezone
        positions = mock_broker.get_positions.return_value
        matching = next((p for p in positions if p.symbol == symbol), positions[0])
        return TickInfo(symbol, matching.current_price, matching.current_price + Decimal(".01"), datetime.now(timezone.utc))
    mock_broker.get_tick.side_effect = management_tick

    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    service._tp1_processed_tickets.add(999889)
    # User explicitly enables multiple partial closes
    service.risk_config.allow_multiple_partial_closes = True

    await service.process_raw_message("Kèo vàng chốt bớt 50%")

    # close_position should be called because allow_multiple_partial_closes is True
    assert mock_broker.close_position.called
    close_call = mock_broker.close_position.call_args[0]
    assert close_call[0] == 999889
    assert close_call[1] == Decimal("0.01")


@pytest.mark.asyncio
async def test_service_combo_chot_bot_and_dich_stoploss(mock_broker):
    """Verify that combo message executes both modify_position (MOVE_SL) and close_position (PARTIAL_CLOSE)."""
    from datetime import datetime, timezone
    from app.broker.base import OrderResult

    pos1 = PositionInfo(
        ticket=342003626,
        symbol="XAUUSDm",
        volume=Decimal("1.00"),
        open_price=Decimal("4090.00"),
        current_price=Decimal("4080.00"),
        sl=Decimal("4120.00"),
        tp=Decimal("4120.00"),
        profit=Decimal("120.00"),
        swap=Decimal("0"),
        commission=Decimal("0"),
        position_type="SELL",
        open_time=datetime.now(timezone.utc),
    )
    pos2 = PositionInfo(
        ticket=342003636,
        symbol="XAUUSDm",
        volume=Decimal("1.00"),
        open_price=Decimal("4095.00"),
        current_price=Decimal("4080.00"),
        sl=Decimal("4120.00"),
        tp=Decimal("4120.00"),
        profit=Decimal("70.00"),
        swap=Decimal("0"),
        commission=Decimal("0"),
        position_type="SELL",
        open_time=datetime.now(timezone.utc),
    )
    pos1.comment = "bot-min-entry"
    pos2.comment = "bot-max-entry"
    mock_broker.get_positions = AsyncMock(return_value=[pos1, pos2])
    mock_broker.modify_position = AsyncMock(return_value=OrderResult(success=True, comment="Modified"))
    mock_broker.close_position = AsyncMock(return_value=OrderResult(success=True, comment="Closed"))

    async def management_tick(symbol):
        from datetime import datetime, timezone
        positions = mock_broker.get_positions.return_value
        matching = next((p for p in positions if p.symbol == symbol), positions[0])
        return TickInfo(symbol, matching.current_price, matching.current_price + Decimal(".01"), datetime.now(timezone.utc))
    mock_broker.get_tick.side_effect = management_tick

    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    raw_msg = (
        "#XAU Khớp Full 2 Entry Cậu Sập +120pips\n"
        "Anh em chủ động chốt bớt entry xấu đi dịch stoploss gồng TP"
    )
    await service.process_raw_message(raw_msg)

    # Explicit SL instruction handles each position once; auto BE is not duplicated.
    assert mock_broker.modify_position.call_count == 2

    # Only Min Entry is partially closed; Max Entry volume remains unchanged.
    assert mock_broker.close_position.call_count == 1
    closed_tickets = [call[0][0] for call in mock_broker.close_position.call_args_list]
    closed_volumes = [call[0][1] for call in mock_broker.close_position.call_args_list]
    assert 342003626 in closed_tickets
    assert 342003636 not in closed_tickets
    assert closed_volumes == [Decimal("0.50")]



@pytest.mark.asyncio
async def test_service_management_rules_add_and_remove(mock_broker):
    service = TradingService(broker=mock_broker, risk_config=RiskConfig())

    # 1. Add new custom Vietnamese keyword rule
    ok = await service.add_or_update_management_rule(
        keyword="cắt bớt",
        action_type_str="PARTIAL_CLOSE",
        percentage=Decimal("40"),
    )
    assert ok is True

    # 2. Check rule engine matched it
    actions = service.parser.rule_engine.match_actions("Kèo vàng chạy rồi cắt bớt nha cả nhà")
    assert len(actions) == 1
    assert actions[0].keyword == "cắt bớt"
    assert actions[0].percentage == Decimal("40")

    # 3. Remove rule
    removed = await service.remove_management_rule("cắt bớt")
    assert removed is True

    # 4. Should no longer match
    actions_after = service.parser.rule_engine.match_actions("Kèo vàng chạy rồi cắt bớt nha cả nhà")
    assert len(actions_after) == 0


@pytest.mark.asyncio
async def test_service_get_closed_deals(mock_broker):
    from datetime import datetime

    deals_sample = [
        DealInfo(
            ticket=101,
            order=201,
            position_id=301,
            symbol="XAUUSD",
            deal_type="BUY",
            entry="OUT",
            volume=Decimal("0.02"),
            price=Decimal("2750.00"),
            profit=Decimal("35.50"),
            swap=Decimal("0"),
            commission=Decimal("-0.20"),
            time=datetime(2026, 10, 3, 10, 30),
        ),
        DealInfo(
            ticket=102,
            order=202,
            position_id=302,
            symbol="EURUSD",
            deal_type="SELL",
            entry="OUT",
            volume=Decimal("0.05"),
            price=Decimal("1.0850"),
            profit=Decimal("-15.00"),
            swap=Decimal("0"),
            commission=Decimal("-0.10"),
            time=datetime(2026, 10, 3, 11, 00),
        ),
        # Deposit deal (should be ignored)
        DealInfo(
            ticket=103,
            order=203,
            position_id=0,
            symbol="",
            deal_type="BALANCE",
            entry="IN",
            volume=Decimal("0"),
            price=Decimal("0"),
            profit=Decimal("1000.00"),
            swap=Decimal("0"),
            commission=Decimal("0"),
            time=datetime(2026, 10, 3, 9, 00),
        ),
    ]
    mock_broker.get_history_deals = AsyncMock(return_value=deals_sample)

    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    closed = await service.get_closed_deals(days=7, limit=10)

    assert len(closed) == 2
    # Newest first
    assert closed[0].ticket == 102
    assert closed[1].ticket == 101


@pytest.mark.asyncio
async def test_service_get_closed_positions_aggregated(mock_broker):
    from datetime import datetime

    deals_sample = [
        # Position 500: Entry deal (0.10 lot BUY XAUUSD @ 2650)
        DealInfo(
            ticket=1,
            order=1001,
            position_id=500,
            symbol="XAUUSD",
            deal_type="BUY",
            entry="IN",
            volume=Decimal("0.10"),
            price=Decimal("2650.00"),
            profit=Decimal("0"),
            swap=Decimal("0"),
            commission=Decimal("-0.50"),
            time=datetime(2026, 10, 3, 9, 0),
        ),
        # Position 500: Partial close 1 (0.05 lot @ 2660.00, profit +50)
        DealInfo(
            ticket=2,
            order=1002,
            position_id=500,
            symbol="XAUUSD",
            deal_type="SELL",
            entry="OUT",
            volume=Decimal("0.05"),
            price=Decimal("2660.00"),
            profit=Decimal("50.00"),
            swap=Decimal("0"),
            commission=Decimal("-0.25"),
            time=datetime(2026, 10, 3, 10, 0),
        ),
        # Position 500: Partial close 2 (remaining 0.05 lot @ 2670.00, profit +100)
        DealInfo(
            ticket=3,
            order=1003,
            position_id=500,
            symbol="XAUUSD",
            deal_type="SELL",
            entry="OUT",
            volume=Decimal("0.05"),
            price=Decimal("2670.00"),
            profit=Decimal("100.00"),
            swap=Decimal("0"),
            commission=Decimal("-0.25"),
            time=datetime(2026, 10, 3, 11, 0),
        ),
        # Position 501: Single exit SELL EURUSD (exit deal is BUY)
        DealInfo(
            ticket=4,
            order=1004,
            position_id=501,
            symbol="EURUSD",
            deal_type="BUY",
            entry="OUT",
            volume=Decimal("0.02"),
            price=Decimal("1.0820"),
            profit=Decimal("-10.00"),
            swap=Decimal("0"),
            commission=Decimal("-0.20"),
            time=datetime(2026, 10, 3, 11, 30),
        ),
    ]
    mock_broker.get_history_deals = AsyncMock(return_value=deals_sample)
    mock_broker.get_positions = AsyncMock(return_value=[])

    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    positions = await service.get_closed_positions(days=7)

    # Must aggregate to 2 positions (not 3 exit deals!)
    assert len(positions) == 2

    # Newest exit first: position 501
    p_eur = positions[0]
    assert p_eur.ticket == 501
    assert p_eur.symbol == "EURUSD"
    assert p_eur.position_type == "SELL"
    assert p_eur.volume == Decimal("0.02")
    assert p_eur.close_price == Decimal("1.0820")
    assert p_eur.profit == Decimal("-10.00")
    assert p_eur.deals_count == 1
    assert p_eur.is_fully_closed is True

    # Position 500: aggregated from 2 partial exits
    p_gold = positions[1]
    assert p_gold.ticket == 500
    assert p_gold.symbol == "XAUUSD"
    assert p_gold.position_type == "BUY"
    assert p_gold.volume == Decimal("0.10")
    assert p_gold.open_price == Decimal("2650.00")
    # Weighted average: (2660 * 0.05 + 2670 * 0.05) / 0.10 = 2665.0
    assert p_gold.close_price == Decimal("2665.00000")
    assert p_gold.profit == Decimal("150.00")
    assert p_gold.commission == Decimal("-1.00")  # -0.50 + -0.25 + -0.25
    assert p_gold.net_pnl == Decimal("149.00")
    assert p_gold.deals_count == 2
    assert p_gold.is_fully_closed is True

    # Performance summary must also reflect 2 positions
    stats = await service.get_performance_summary(days=7)
    assert stats["total_trades"] == 2
    assert stats["win_trades"] == 1
    assert stats["loss_trades"] == 1
    assert stats["win_rate"] == 50.0
    assert stats["total_profit"] == Decimal("150.00")
    assert stats["total_loss"] == Decimal("-10.00")
    assert stats["best_trade"] == Decimal("150.00")
    assert stats["worst_trade"] == Decimal("-10.00")


@pytest.mark.asyncio
async def test_service_get_closed_positions_partially_closed_active(mock_broker):
    from datetime import datetime

    deals_sample = [
        DealInfo(
            ticket=10,
            order=2001,
            position_id=600,
            symbol="XAUUSD",
            deal_type="SELL",
            entry="OUT",
            volume=Decimal("0.05"),
            price=Decimal("2660.00"),
            profit=Decimal("50.00"),
            swap=Decimal("0"),
            commission=Decimal("-0.25"),
            time=datetime(2026, 10, 3, 10, 0),
        ),
    ]
    mock_broker.get_history_deals = AsyncMock(return_value=deals_sample)
    # Position 600 is still running in open positions!
    active_pos = PositionInfo(
        ticket=600,
        symbol="XAUUSD",
        volume=Decimal("0.05"),
        open_price=Decimal("2650.00"),
        current_price=Decimal("2665.00"),
        sl=Decimal("2650.00"),
        tp=Decimal("2680.00"),
        profit=Decimal("75.00"),
        swap=Decimal("0"),
        commission=Decimal("-0.25"),
        position_type="BUY",
        open_time=datetime(2026, 10, 3, 9, 0),
    )
    mock_broker.get_positions = AsyncMock(return_value=[active_pos])

    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    positions = await service.get_closed_positions(days=7)

    assert len(positions) == 1
    p = positions[0]
    assert p.ticket == 600
    assert p.is_fully_closed is False  # Still active
    assert p.position_type == "BUY"  # Inherited from active position
    assert p.open_price == Decimal("2650.00")
    assert p.volume == Decimal("0.05")
    assert p.deals_count == 1


@pytest.mark.asyncio
async def test_map_deal_info_mt5_mac_and_windows():
    from unittest.mock import MagicMock
    from app.broker.mt5.mapper import map_deal_info

    # 1. mt5_mac namedtuple structure with `position` field
    raw_mac = MagicMock(spec=["position", "ticket", "order", "symbol", "type", "entry", "volume", "price", "profit", "swap", "commission", "time", "reason", "magic", "comment"])
    raw_mac.position = 777888
    raw_mac.ticket = 1001
    raw_mac.order = 2001
    raw_mac.symbol = "XAUUSD"
    raw_mac.type = 1  # SELL
    raw_mac.entry = 1  # OUT
    raw_mac.volume = 0.05
    raw_mac.price = 2755.0
    raw_mac.profit = 45.0
    raw_mac.swap = 0.0
    raw_mac.commission = -0.5
    raw_mac.time = 1700000000
    raw_mac.reason = 5
    raw_mac.magic = 234000
    raw_mac.comment = "[tp 2755.00]"

    deal_mac = map_deal_info(raw_mac)
    assert deal_mac.position_id == 777888
    assert deal_mac.deal_type == "SELL"
    assert deal_mac.entry == "OUT"
    assert deal_mac.volume == Decimal("0.05")

    # 2. Windows MetaTrader5 structure with `position_id` field
    raw_win = MagicMock(spec=["position_id", "ticket", "order", "symbol", "type", "entry", "volume", "price", "profit", "swap", "commission", "time", "reason", "magic", "comment"])
    raw_win.position_id = 999111
    raw_win.ticket = 1002
    raw_win.order = 2002
    raw_win.symbol = "EURUSD"
    raw_win.type = 0  # BUY
    raw_win.entry = 0  # IN
    raw_win.volume = 0.10
    raw_win.price = 1.0850
    raw_win.profit = 0.0
    raw_win.swap = 0.0
    raw_win.commission = -0.7
    raw_win.time = 1700000000
    raw_win.reason = 3
    raw_win.magic = 234000
    raw_win.comment = ""

    deal_win = map_deal_info(raw_win)
    assert deal_win.position_id == 999111
    assert deal_win.deal_type == "BUY"
    assert deal_win.entry == "IN"


@pytest.mark.asyncio
async def test_service_get_closed_positions_comment_fallback(mock_broker):
    from datetime import datetime

    deals_sample = [
        DealInfo(
            ticket=201,
            order=301,
            position_id=0,
            symbol="XAUUSD",
            deal_type="SELL",
            entry="OUT",
            volume=Decimal("0.03"),
            price=Decimal("2660.00"),
            profit=Decimal("30.00"),
            swap=Decimal("0"),
            commission=Decimal("-0.30"),
            time=datetime(2026, 10, 3, 10, 0),
            comment="close #998877",
        ),
        DealInfo(
            ticket=202,
            order=302,
            position_id=0,
            symbol="XAUUSD",
            deal_type="SELL",
            entry="OUT",
            volume=Decimal("0.02"),
            price=Decimal("2670.00"),
            profit=Decimal("40.00"),
            swap=Decimal("0"),
            commission=Decimal("-0.20"),
            time=datetime(2026, 10, 3, 11, 0),
            comment="close #998877",
        ),
    ]
    mock_broker.get_history_deals = AsyncMock(return_value=deals_sample)
    mock_broker.get_positions = AsyncMock(return_value=[])

    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    positions = await service.get_closed_positions(days=7)

    assert len(positions) == 1
    assert positions[0].ticket == 998877
    assert positions[0].deals_count == 2
    assert positions[0].volume == Decimal("0.05")
    assert positions[0].profit == Decimal("70.00")


@pytest.mark.asyncio
async def test_service_get_performance_summary(mock_broker):
    from datetime import datetime

    deals_sample = [
        DealInfo(
            ticket=101,
            order=201,
            position_id=301,
            symbol="XAUUSD",
            deal_type="BUY",
            entry="OUT",
            volume=Decimal("0.02"),
            price=Decimal("2750.00"),
            profit=Decimal("50.00"),
            swap=Decimal("0"),
            commission=Decimal("-0.50"),
            time=datetime(2026, 10, 3, 10, 30),
        ),
        DealInfo(
            ticket=102,
            order=202,
            position_id=302,
            symbol="EURUSD",
            deal_type="SELL",
            entry="OUT",
            volume=Decimal("0.05"),
            price=Decimal("1.0850"),
            profit=Decimal("-25.00"),
            swap=Decimal("0"),
            commission=Decimal("-0.50"),
            time=datetime(2026, 10, 3, 11, 00),
        ),
    ]
    mock_broker.get_history_deals = AsyncMock(return_value=deals_sample)

    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    stats = await service.get_performance_summary(days=7)

    assert stats["total_trades"] == 2
    assert stats["win_trades"] == 1
    assert stats["loss_trades"] == 1
    assert stats["win_rate"] == 50.0
    assert stats["total_profit"] == Decimal("50.00")
    assert stats["total_loss"] == Decimal("-25.00")
    assert stats["profit_factor"] == 2.0
    assert stats["net_pnl"] == Decimal("24.00")  # 50 - 25 - 0.50 - 0.50
    assert stats["best_trade"] == Decimal("50.00")
    assert stats["worst_trade"] == Decimal("-25.00")


@pytest.mark.asyncio
async def test_service_tp1_and_tp2_milestones(mock_broker):
    from datetime import datetime
    from app.models.trade import Order as DBOrder

    mock_broker.close_position = AsyncMock(return_value=OrderResult(success=True, comment="Closed"))
    mock_broker.modify_position = AsyncMock(return_value=OrderResult(success=True, comment="Modified"))

    async def management_tick(symbol):
        from datetime import datetime, timezone
        positions = mock_broker.get_positions.return_value
        matching = next((p for p in positions if p.symbol == symbol), positions[0])
        return TickInfo(symbol, matching.current_price, matching.current_price + Decimal(".01"), datetime.now(timezone.utc))
    mock_broker.get_tick.side_effect = management_tick

    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    notifications = []
    service.set_notification_callback(lambda msg: notifications.append(msg))

    mock_order = DBOrder(
        signal_id=1,
        mt5_ticket=2001,
        symbol="XAUUSD",
        volume=Decimal("0.02"),
        tp1=Decimal("4050"),
        tp2=Decimal("4060"),
    )

    class MockScalars:
        def all(self):
            return [mock_order]

    class MockSession:
        async def scalars(self, stmt):
            return MockScalars()
        async def execute(self, stmt):
            result = MagicMock()
            result.all.return_value = []
            return result
        async def scalar(self, stmt):
            return 1
        def add(self, obj):
            pass

    class MockCtx:
        async def __aenter__(self):
            return MockSession()
        async def __aexit__(self, *args):
            pass

    with patch("app.config.database.get_session", return_value=MockCtx()):
        # 1. At TP1 (4052 >= 4050): Partial close 50% & move SL to entry
        pos = PositionInfo(
            ticket=2001,
            symbol="XAUUSD",
            volume=Decimal("0.02"),
            open_price=Decimal("4040"),
            current_price=Decimal("4052"),
            sl=Decimal("4030"),
            tp=Decimal("4060"),
            profit=Decimal("24.0"),
            swap=Decimal("0"),
            commission=Decimal("0"),
            position_type="BUY",
            open_time=datetime.now(),
        )
        mock_broker.get_positions = AsyncMock(return_value=[pos])

        await service._check_tp_milestones()
        assert mock_broker.close_position.call_count == 1
        assert mock_broker.close_position.call_args[0] == (2001, Decimal("0.01"))
        assert mock_broker.modify_position.call_count == 1
        assert mock_broker.modify_position.call_args[1]["sl"] == Decimal("4040")

        # 2. Still around TP1, but already closed once -> should skip
        pos_after = PositionInfo(
            ticket=2001,
            symbol="XAUUSD",
            volume=Decimal("0.01"),
            open_price=Decimal("4040"),
            current_price=Decimal("4052"),
            sl=Decimal("4040"),
            tp=Decimal("4060"),
            profit=Decimal("12.0"),
            swap=Decimal("0"),
            commission=Decimal("0"),
            position_type="BUY",
            open_time=datetime.now(),
        )
        mock_broker.get_positions = AsyncMock(return_value=[pos_after])
        mock_broker.close_position.reset_mock()
        mock_broker.modify_position.reset_mock()

        await service._check_tp_milestones()
        assert mock_broker.close_position.call_count == 0
        assert mock_broker.modify_position.call_count == 0

        # 3. At TP2 (4062 >= 4060) -> full close 100%
        pos_tp2 = PositionInfo(
            ticket=2001,
            symbol="XAUUSD",
            volume=Decimal("0.01"),
            open_price=Decimal("4040"),
            current_price=Decimal("4062"),
            sl=Decimal("4040"),
            tp=Decimal("4060"),
            profit=Decimal("22.0"),
            swap=Decimal("0"),
            commission=Decimal("0"),
            position_type="BUY",
            open_time=datetime.now(),
        )
        mock_broker.get_positions = AsyncMock(return_value=[pos_tp2])
        mock_broker.close_position.reset_mock()

        await service._check_tp_milestones()
        assert mock_broker.close_position.call_count == 1
        assert mock_broker.close_position.call_args[0] == (2001, Decimal("0.01"))


@pytest.mark.asyncio
async def test_service_buy_dual_entry_min_and_max_entry_handling(mock_broker):
    from datetime import datetime, timezone
    # BUY 2750 - 2740, SL 2730
    # min_entry is 2750 (xa SL), max_entry is 2740 (gần SL)
    # Current price is 2749.0 (inside entry zone, favorable compared to 2750)
    mock_broker.get_tick = AsyncMock(
        return_value=TickInfo(
            symbol="XAUUSDm",
            bid=Decimal("2749.0"),
            ask=Decimal("2749.5"),
            time=datetime.now(timezone.utc),
        )
    )
    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    notifications = []
    service.set_notification_callback(lambda msg: notifications.append(msg))

    raw_signal = "BUY #XAU 2750 - 2740\nSL: 2730\nTP: 2780"
    await service.process_raw_message(raw_signal)

    # 2 orders should be placed:
    # Order 1: Market BUY at 2749.0 (Min Entry)
    # Order 2: BUY_LIMIT at 2740 (Max Entry, gần SL)
    assert mock_broker.open_order.call_count == 2
    call_req_1 = mock_broker.open_order.call_args_list[0][0][0]
    assert call_req_1.order_type == "BUY"
    assert call_req_1.price == Decimal("2749.5")  # Ask price for BUY order

    call_req_2 = mock_broker.open_order.call_args_list[1][0][0]
    assert call_req_2.order_type == "BUY_LIMIT"
    assert call_req_2.price == Decimal("2740")  # Max Entry (gần SL)


@pytest.mark.asyncio
async def test_duplicate_signal_asks_user_confirmation(mock_broker):
    """When a duplicate signal is received, bot should ask user with interactive buttons."""
    from datetime import datetime, timezone

    mock_broker.get_tick = AsyncMock(
        return_value=TickInfo(
            symbol="XAUUSDm",
            bid=Decimal("2650.0"),
            ask=Decimal("2650.5"),
            time=datetime.now(timezone.utc),
        )
    )
    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    notifications = []

    async def mock_notify(msg: str, reply_markup=None):
        notifications.append((msg, reply_markup))

    service.set_notification_callback(mock_notify)

    raw_signal = "BUY #XAU 2650 - 2648\nSL: 2640\nTP: 2660"

    # First signal succeeds
    await service.process_raw_message(raw_signal)
    assert len(notifications) >= 1
    assert mock_broker.open_order.call_count >= 1

    # Second identical signal sent immediately (within 60s timeout)
    notifications.clear()
    await service.process_raw_message(raw_signal)

    # Instead of flat reject, it must ask user with override buttons
    assert len(notifications) == 1
    msg, kb = notifications[0]
    assert "Cảnh báo: Trùng Tín Hiệu (Duplicate Signal)" in msg
    assert "Tạm dừng đặt lệnh tự động để bạn quyết định!" in msg
    assert kb is not None  # Keyboard with buttons
    assert len(service._pending_overrides) >= 1

    # Now simulate user clicking override button
    sig_id = list(service._pending_overrides.keys())[0]
    res_msg = await service.execute_override_order(sig_id, "both")
    assert "✅ BUY XAUUSD" in res_msg
    assert "✅ BUY_LIMIT XAUUSD" in res_msg


@pytest.mark.asyncio
async def test_max_positions_asks_user_confirmation(mock_broker):
    """When max open positions reached, bot should ask user with interactive buttons."""
    from datetime import datetime, timezone

    # Simulate 5 open positions
    mock_broker.get_positions = AsyncMock(
        return_value=[
            PositionInfo(
                ticket=100 + i,
                symbol="XAUUSD",
                volume=Decimal("0.01"),
                open_price=Decimal("2650"),
                current_price=Decimal("2651"),
                sl=None,
                tp=None,
                profit=Decimal("10"),
                swap=Decimal("0"),
                commission=Decimal("0"),
                position_type="BUY",
                open_time=datetime.now(timezone.utc),
            )
            for i in range(5)
        ]
    )
    mock_broker.get_tick = AsyncMock(
        return_value=TickInfo(
            symbol="XAUUSD",
            bid=Decimal("2650.0"),
            ask=Decimal("2650.5"),
            time=datetime.now(timezone.utc),
        )
    )

    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    service.risk_config.max_open_positions = 5
    notifications = []

    async def mock_notify(msg: str, reply_markup=None):
        notifications.append((msg, reply_markup))

    service.set_notification_callback(mock_notify)

    raw_signal = "BUY #XAU 2650 - 2648\nSL: 2640\nTP: 2660"
    await service.process_raw_message(raw_signal)

    # Must ask user
    assert len(notifications) == 1
    msg, kb = notifications[0]
    assert "Cảnh báo: Đạt Giới Hạn Vị Thế (Max Positions)" in msg
    assert "Tạm dừng đặt lệnh tự động để bạn quyết định!" in msg
    assert kb is not None
    assert len(service._pending_overrides) >= 1


@pytest.mark.asyncio
async def test_reply_position_management_targets_only_replied_signal_positions(mock_broker):
    """Verify that a position management signal replying to a previous signal
    only affects the orders/positions tied to that specific signal."""
    from datetime import datetime, timezone
    from app.broker.base import PendingOrderInfo

    pos_sig1_a = PositionInfo(
        ticket=101,
        symbol="XAUUSD",
        volume=Decimal("0.01"),
        open_price=Decimal("2650.00"),
        current_price=Decimal("2660.00"),
        sl=Decimal("2640.00"),
        tp=Decimal("2670.00"),
        profit=Decimal("10.00"),
        swap=Decimal("0"),
        commission=Decimal("0"),
        position_type="BUY",
        open_time=datetime.now(timezone.utc),
    )
    pos_sig1_b = PositionInfo(
        ticket=102,
        symbol="XAUUSD",
        volume=Decimal("0.02"),
        open_price=Decimal("2648.00"),
        current_price=Decimal("2660.00"),
        sl=Decimal("2640.00"),
        tp=Decimal("2670.00"),
        profit=Decimal("24.00"),
        swap=Decimal("0"),
        commission=Decimal("0"),
        position_type="BUY",
        open_time=datetime.now(timezone.utc),
    )
    pos_sig2 = PositionInfo(
        ticket=201,
        symbol="XAUUSD",
        volume=Decimal("0.05"),
        open_price=Decimal("2655.00"),
        current_price=Decimal("2660.00"),
        sl=Decimal("2645.00"),
        tp=Decimal("2680.00"),
        profit=Decimal("25.00"),
        swap=Decimal("0"),
        commission=Decimal("0"),
        position_type="BUY",
        open_time=datetime.now(timezone.utc),
    )

    pending_sig1 = PendingOrderInfo(
        ticket=103,
        symbol="XAUUSD",
        order_type="BUY_LIMIT",
        volume=Decimal("0.01"),
        price=Decimal("2645.00"),
        sl=Decimal("2640.00"),
        tp=Decimal("2670.00"),
        comment="",
        setup_time=datetime.now(timezone.utc),
    )
    pending_sig2 = PendingOrderInfo(
        ticket=202,
        symbol="XAUUSD",
        order_type="BUY_LIMIT",
        volume=Decimal("0.02"),
        price=Decimal("2652.00"),
        sl=Decimal("2645.00"),
        tp=Decimal("2680.00"),
        comment="",
        setup_time=datetime.now(timezone.utc),
    )

    mock_broker.get_positions = AsyncMock(return_value=[pos_sig1_a, pos_sig1_b, pos_sig2])
    mock_broker.get_pending_orders = AsyncMock(return_value=[pending_sig1, pending_sig2])
    mock_broker.close_position = AsyncMock(return_value=OrderResult(success=True, ticket=101, comment="Closed"))
    mock_broker.cancel_order = AsyncMock(return_value=OrderResult(success=True, ticket=103, comment="Cancelled"))

    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    # Map tickets to signal messages
    service._signal_msg_id_to_tickets[1000] = [101, 102, 103]
    service._signal_msg_id_to_tickets[2000] = [201, 202]

    notifications = []
    async def mock_notify(msg: str, reply_markup=None):
        notifications.append(msg)
    service.set_notification_callback(mock_notify)

    # Management command: "Đóng lệnh" replying to message 1000
    await service.process_raw_message("Đóng lệnh", reply_to_msg_id=1000)

    # 1. Verify positions closed ONLY for tickets 101 and 102
    closed_tickets = [call.args[0] for call in mock_broker.close_position.call_args_list]
    assert 101 in closed_tickets
    assert 102 in closed_tickets
    assert 201 not in closed_tickets

    # 2. Verify pending order 103 cancelled, but 202 untouched
    cancelled_tickets = [call.args[0] for call in mock_broker.cancel_order.call_args_list]
    assert 103 in cancelled_tickets
    assert 202 not in cancelled_tickets

    # 3. Verify notification includes Signal #1000
    assert any("Signal #1000" in n for n in notifications)


@pytest.mark.asyncio
async def test_reply_position_management_uses_each_ticket_entry(mock_broker):
    """Verify that Breakeven dời SL calculates VWAP of only the target signal's positions."""
    from datetime import datetime, timezone

    # Signal 1000 has 2 positions:
    # 0.01 lot @ 2650.00
    # 0.02 lot @ 2640.00
    # Different entry prices must remain independent, even within one setup.
    pos_1 = PositionInfo(
        ticket=101,
        symbol="XAUUSD",
        volume=Decimal("0.01"),
        open_price=Decimal("2650.00"),
        current_price=Decimal("2660.00"),
        sl=Decimal("2630.00"),
        tp=Decimal("2680.00"),
        profit=Decimal("10.00"),
        swap=Decimal("0"),
        commission=Decimal("0"),
        position_type="BUY",
        open_time=datetime.now(timezone.utc),
    )
    pos_2 = PositionInfo(
        ticket=102,
        symbol="XAUUSD",
        volume=Decimal("0.02"),
        open_price=Decimal("2640.00"),
        current_price=Decimal("2660.00"),
        sl=Decimal("2630.00"),
        tp=Decimal("2680.00"),
        profit=Decimal("40.00"),
        swap=Decimal("0"),
        commission=Decimal("0"),
        position_type="BUY",
        open_time=datetime.now(timezone.utc),
    )
    # Unrelated position from another signal at 2700
    pos_other = PositionInfo(
        ticket=999,
        symbol="XAUUSD",
        volume=Decimal("0.10"),
        open_price=Decimal("2700.00"),
        current_price=Decimal("2660.00"),
        sl=Decimal("2650.00"),
        tp=Decimal("2750.00"),
        profit=Decimal("-400.00"),
        swap=Decimal("0"),
        commission=Decimal("0"),
        position_type="BUY",
        open_time=datetime.now(timezone.utc),
    )

    mock_broker.get_positions = AsyncMock(return_value=[pos_1, pos_2, pos_other])
    mock_broker.get_pending_orders = AsyncMock(return_value=[])
    mock_broker.modify_position = AsyncMock(return_value=OrderResult(success=True, ticket=101, comment="Modified"))

    async def management_tick(symbol):
        from datetime import datetime, timezone
        positions = mock_broker.get_positions.return_value
        matching = next((p for p in positions if p.symbol == symbol), positions[0])
        return TickInfo(symbol, matching.current_price, matching.current_price + Decimal(".01"), datetime.now(timezone.utc))
    mock_broker.get_tick.side_effect = management_tick

    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    service.risk_config.breakeven_offset_price = Decimal("0.0")
    service._signal_msg_id_to_tickets[1000] = [101, 102]

    # Command: "Dời SL về hòa" replying to message 1000
    await service.process_raw_message("Dời SL về hòa", reply_to_msg_id=1000)

    # Each ticket moves to its own entry; no shared average is used.

    modified_calls = mock_broker.modify_position.call_args_list
    assert len(modified_calls) == 2
    for call in modified_calls:
        ticket = call.args[0]
        sl = call.kwargs["sl"]
        assert ticket in (101, 102)
        assert sl == {101: Decimal("2650"), 102: Decimal("2640")}[ticket]


@pytest.mark.asyncio
async def test_reply_position_management_already_closed(mock_broker):
    """Verify notification when replied-to signal positions are already closed."""
    from datetime import datetime, timezone

    # Only an unrelated position is open
    pos_other = PositionInfo(
        ticket=999,
        symbol="XAUUSD",
        volume=Decimal("0.01"),
        open_price=Decimal("2650.00"),
        current_price=Decimal("2660.00"),
        sl=Decimal("2640.00"),
        tp=Decimal("2680.00"),
        profit=Decimal("10.00"),
        swap=Decimal("0"),
        commission=Decimal("0"),
        position_type="BUY",
        open_time=datetime.now(timezone.utc),
    )

    mock_broker.get_positions = AsyncMock(return_value=[pos_other])
    mock_broker.get_pending_orders = AsyncMock(return_value=[])

    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    service._signal_msg_id_to_tickets[1000] = [101, 102]

    notifications = []
    async def mock_notify(msg: str, reply_markup=None):
        notifications.append(msg)
    service.set_notification_callback(mock_notify)

    # Command replying to message 1000 whose tickets 101, 102 are already closed
    await service.process_raw_message("Đóng lệnh", reply_to_msg_id=1000)

    assert len(notifications) == 1
    assert "đã được đóng trước đó" in notifications[0]
    assert "Signal #1000" in notifications[0]


@pytest.mark.asyncio
async def test_dual_entry_cancels_remaining_pending_limit_when_position_closes(mock_broker):
    """Verify that in a dual-entry setup, closing one position (Min Entry)
    automatically cancels the remaining pending limit order (Max Entry)."""
    from datetime import datetime, timezone
    from app.broker.base import PendingOrderInfo

    pos_min = PositionInfo(
        ticket=1001,
        symbol="XAUUSD",
        volume=Decimal("0.01"),
        open_price=Decimal("2650.00"),
        current_price=Decimal("2660.00"),
        sl=Decimal("2640.00"),
        tp=Decimal("2670.00"),
        profit=Decimal("10.00"),
        swap=Decimal("0"),
        commission=Decimal("0"),
        position_type="BUY",
        open_time=datetime.now(timezone.utc),
    )
    pending_max = PendingOrderInfo(
        ticket=1002,
        symbol="XAUUSD",
        order_type="BUY_LIMIT",
        volume=Decimal("0.01"),
        price=Decimal("2645.00"),
        sl=Decimal("2640.00"),
        tp=Decimal("2670.00"),
        comment="bot-max-entry",
        setup_time=datetime.now(timezone.utc),
    )

    mock_broker.get_positions = AsyncMock(return_value=[])
    mock_broker.get_pending_orders = AsyncMock(return_value=[pending_max])
    mock_broker.close_position = AsyncMock(return_value=OrderResult(success=True, ticket=1001, comment="Closed"))
    mock_broker.cancel_order = AsyncMock(return_value=OrderResult(success=True, ticket=1002, comment="Cancelled"))

    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    # Both orders belong to signal 500
    service._signal_db_id_to_tickets[500] = [1001, 1002]
    service._ticket_to_signal_db_id[1001] = 500
    service._ticket_to_signal_db_id[1002] = 500

    notifications = []
    async def mock_notify(msg: str, reply_markup=None):
        notifications.append(msg)
    service.set_notification_callback(mock_notify)

    # 1. Close Min Entry position manually / via service
    res = await service.close_position_by_ticket(1001)
    assert res.success

    # 2. Assert pending limit order 1002 was cancelled
    assert mock_broker.cancel_order.called
    mock_broker.cancel_order.assert_awaited_once_with(1002)

    # 3. Notification sent
    assert any("Hủy Lệnh Chờ Dual Entry" in n and "1002" in n for n in notifications)


@pytest.mark.asyncio
async def test_dual_entry_both_filled_does_not_cancel(mock_broker):
    """Verify that if both dual-entry orders have already filled into positions,
    closing one does not attempt to cancel the other."""
    from datetime import datetime, timezone

    pos_min = PositionInfo(
        ticket=1001,
        symbol="XAUUSD",
        volume=Decimal("0.01"),
        open_price=Decimal("2650.00"),
        current_price=Decimal("2660.00"),
        sl=Decimal("2640.00"),
        tp=Decimal("2670.00"),
        profit=Decimal("10.00"),
        swap=Decimal("0"),
        commission=Decimal("0"),
        position_type="BUY",
        open_time=datetime.now(timezone.utc),
    )
    pos_max = PositionInfo(
        ticket=1002,
        symbol="XAUUSD",
        volume=Decimal("0.01"),
        open_price=Decimal("2645.00"),
        current_price=Decimal("2660.00"),
        sl=Decimal("2640.00"),
        tp=Decimal("2670.00"),
        profit=Decimal("15.00"),
        swap=Decimal("0"),
        commission=Decimal("0"),
        position_type="BUY",
        open_time=datetime.now(timezone.utc),
    )

    mock_broker.get_positions = AsyncMock(return_value=[pos_min, pos_max])
    mock_broker.get_pending_orders = AsyncMock(return_value=[])
    mock_broker.close_position = AsyncMock(return_value=OrderResult(success=True, ticket=1001, comment="Closed"))
    mock_broker.cancel_order = AsyncMock()

    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    service._signal_db_id_to_tickets[500] = [1001, 1002]
    service._ticket_to_signal_db_id[1001] = 500
    service._ticket_to_signal_db_id[1002] = 500

    # Close Min Entry position
    await service.close_position_by_ticket(1001)

    # Assert cancel_order was NOT called because 1002 is not a pending order
    assert not mock_broker.cancel_order.called


@pytest.mark.asyncio
async def test_reconcile_dual_entry_pending_orders_cancels_when_sibling_closed(mock_broker):
    """Verify that _reconcile_dual_entry_pending_orders automatically discovers
    a pending limit order whose sibling position has closed, and cancels it."""
    from datetime import datetime, timezone
    from app.broker.base import PendingOrderInfo

    # Sibling position 1001 is completely gone (neither open nor pending)
    # Pending limit order 1002 is still waiting
    pending_max = PendingOrderInfo(
        ticket=1002,
        symbol="XAUUSD",
        order_type="BUY_LIMIT",
        volume=Decimal("0.01"),
        price=Decimal("2645.00"),
        sl=Decimal("2640.00"),
        tp=Decimal("2670.00"),
        comment="bot-max-entry",
        setup_time=datetime.now(timezone.utc),
    )

    mock_broker.get_positions = AsyncMock(return_value=[])  # No open positions
    mock_broker.get_pending_orders = AsyncMock(return_value=[pending_max])
    mock_broker.cancel_order = AsyncMock(return_value=OrderResult(success=True, ticket=1002, comment="Cancelled"))

    mock_broker.get_history_deals.return_value = [MagicMock(position_id=1001, entry="OUT")]

    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    service._signal_db_id_to_tickets[500] = [1001, 1002]
    service._ticket_to_signal_db_id[1002] = 500

    notifications = []
    async def mock_notify(msg: str, reply_markup=None):
        notifications.append(msg)
    service.set_notification_callback(mock_notify)

    # Run reconciliation
    cancelled = await service._reconcile_dual_entry_pending_orders()

    assert 1002 in cancelled
    mock_broker.cancel_order.assert_awaited_once_with(1002)
    assert any("Hủy Lệnh Chờ Dual Entry" in n and "1002" in n for n in notifications)


@pytest.mark.asyncio
async def test_personal_bot_format_history_message(mock_broker):
    from datetime import datetime
    from app.telegram.personal_bot import PersonalTelegramBot

    # Simulate 2 partial closes for Position 888
    deals = [
        DealInfo(
            ticket=11,
            order=3001,
            position_id=888,
            symbol="XAUUSD",
            deal_type="BUY",
            entry="IN",
            volume=Decimal("0.10"),
            price=Decimal("2650.00"),
            profit=Decimal("0"),
            swap=Decimal("0"),
            commission=Decimal("-0.50"),
            time=datetime(2026, 10, 3, 8, 0),
        ),
        DealInfo(
            ticket=12,
            order=3002,
            position_id=888,
            symbol="XAUUSD",
            deal_type="SELL",
            entry="OUT",
            volume=Decimal("0.05"),
            price=Decimal("2660.00"),
            profit=Decimal("50.00"),
            swap=Decimal("0"),
            commission=Decimal("-0.25"),
            time=datetime(2026, 10, 3, 9, 0),
        ),
        DealInfo(
            ticket=13,
            order=3003,
            position_id=888,
            symbol="XAUUSD",
            deal_type="SELL",
            entry="OUT",
            volume=Decimal("0.05"),
            price=Decimal("2670.00"),
            profit=Decimal("100.00"),
            swap=Decimal("0"),
            commission=Decimal("-0.25"),
            time=datetime(2026, 10, 3, 10, 0),
        ),
    ]
    mock_broker.get_history_deals = AsyncMock(return_value=deals)
    mock_broker.get_positions = AsyncMock(return_value=[])

    service = TradingService(broker=mock_broker, risk_config=RiskConfig())
    bot = PersonalTelegramBot(service=service)

    msg, keyboard = await bot._format_history_message(days=7)

    # Summary stays compact; fees and individual exits are behind the detail button.
    assert "XAUUSD · BUY" in msg
    assert "+149.00 USD" in msg
    assert "Đóng 0.1/0.1 lot" in msg
    assert "2 lần chốt" in msg
    assert "Entry 2,650.00 → Đóng TB 2,665.00" in msg
    assert "10:00 03/10/2026" in msg
    assert "#888" in msg
    assert "2,660.00" not in msg
    assert "history_detail_7_0_888_0" in [b.callback_data for row in keyboard.inline_keyboard for b in row]

    detail, keyboard = await bot._format_history_detail(7, 0, 888)
    assert "Commission" in detail
    assert "0.05 lot @ 2,660.00" in detail
    assert "0.05 lot @ 2,670.00" in detail
    assert "+49.75 USD" in detail and "+99.75 USD" in detail
    assert detail.index("2,670.00") < detail.index("2,660.00")
    assert keyboard.inline_keyboard[-1][0].callback_data == "history_7_0"
