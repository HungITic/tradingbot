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
    return broker


@pytest.mark.asyncio
async def test_service_executes_new_order_signal(mock_broker):
    service = TradingService(broker=mock_broker)
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
    service = TradingService(broker=mock_broker)
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
    service = TradingService(broker=mock_broker)
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
    service = TradingService(broker=mock_broker)
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
    service = TradingService(broker=mock_broker)
    notifications = []
    service.set_notification_callback(lambda msg: notifications.append(msg))

    raw_signal = "Sell #XAU 4148 - 4160\nSL: 4170\nTP: Trên 200pips"
    await service.process_raw_message(raw_signal)

    # When excessive slippage, execution is paused for user decision (0 orders placed automatically)
    assert mock_broker.open_order.call_count == 0

    # Warning alert must be in notifications
    assert any("Cảnh báo Min Entry: Vượt ngưỡng trượt giá" in n for n in notifications)


@pytest.mark.asyncio
async def test_service_middle_zone_executes_only_one_market_order(mock_broker):
    from datetime import datetime, timezone
    # SELL 4148 - 4160, current price is 4154 (exact midpoint)
    mock_broker.get_tick = AsyncMock(
        return_value=TickInfo(
            symbol="XAUUSDm",
            bid=Decimal("4154.0"),
            ask=Decimal("4154.5"),
            time=datetime.now(timezone.utc),
        )
    )
    service = TradingService(broker=mock_broker)
    notifications = []
    service.set_notification_callback(lambda msg: notifications.append(msg))

    raw_signal = "Sell #XAU 4148 - 4160\nSL: 4170\nTP: Trên 200pips"
    await service.process_raw_message(raw_signal)

    # Only 1 market order should be placed, NO pending limit at 4160
    assert mock_broker.open_order.call_count == 1
    call_req = mock_broker.open_order.call_args[0][0]
    assert call_req.order_type == "SELL"
    assert call_req.price == Decimal("4154.0")
    assert call_req.comment == "bot-mid-entry"

    assert len(notifications) == 1
    assert "Order Executed (Mid Entry)" in notifications[0]
    assert "khoảng giữa vùng Entry" in notifications[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("price_str", ["4153.0", "4155.0"])
async def test_service_middle_zone_boundary_executes_only_one_market_order(mock_broker, price_str):
    from datetime import datetime, timezone
    mock_broker.get_tick = AsyncMock(
        return_value=TickInfo(
            symbol="XAUUSDm",
            bid=Decimal(price_str),
            ask=Decimal(price_str) + Decimal("0.5"),
            time=datetime.now(timezone.utc),
        )
    )
    service = TradingService(broker=mock_broker)
    notifications = []
    service.set_notification_callback(lambda msg: notifications.append(msg))

    raw_signal = "Sell #XAU 4148 - 4160\nSL: 4170\nTP: Trên 200pips"
    await service.process_raw_message(raw_signal)

    # Only 1 market order should be placed
    assert mock_broker.open_order.call_count == 1
    call_req = mock_broker.open_order.call_args[0][0]
    assert call_req.order_type == "SELL"
    assert call_req.price == Decimal(price_str)
    assert "Order Executed (Mid Entry)" in notifications[0]


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
    service = TradingService(broker=mock_broker)
    notifications = []
    service.set_notification_callback(lambda msg: notifications.append(msg))

    raw_signal = "Sell #XAU 4148 - 4160\nSL: 4170\nTP: Trên 200pips"
    await service.process_raw_message(raw_signal)

    # No order should be placed at all!
    assert mock_broker.open_order.call_count == 0
    assert any("Giá hiện tại vượt quá Max Entry" in n for n in notifications)


@pytest.mark.asyncio
async def test_service_rejects_signal_when_paused(mock_broker):
    service = TradingService(broker=mock_broker)
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

    service = TradingService(broker=mock_broker)
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
    assert "Position Management: Close" in notifications[0]
    assert "Auto Breakeven on Partial" in notifications[1]


@pytest.mark.asyncio
async def test_service_management_rules_add_and_remove(mock_broker):
    service = TradingService(broker=mock_broker)

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

    service = TradingService(broker=mock_broker)
    closed = await service.get_closed_deals(days=7, limit=10)

    assert len(closed) == 2
    # Newest first
    assert closed[0].ticket == 102
    assert closed[1].ticket == 101


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

    service = TradingService(broker=mock_broker)
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

    service = TradingService(broker=mock_broker)
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
    service = TradingService(broker=mock_broker)
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




