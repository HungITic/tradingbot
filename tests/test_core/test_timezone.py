"""
Unit tests for app.core.timezone and MT5 broker timezone mapping.
"""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.broker.mt5.mapper import (
    map_deal_info,
    map_pending_order_info,
    map_position_info,
    map_tick_info,
)
from app.core.timezone import (
    VN_TZ,
    broker_timestamp_to_datetime,
    detect_and_update_broker_offset,
    format_vietnam_time,
    get_broker_utc_offset_hours,
    now_vietnam,
    set_broker_utc_offset_hours,
    to_vietnam_datetime,
)


def test_now_vietnam():
    """Verify now_vietnam returns timezone-aware datetime in UTC+7."""
    vn = now_vietnam()
    assert vn.tzinfo is not None
    # Offset should be +7 hours (+25200 seconds)
    assert vn.utcoffset().total_seconds() == 7 * 3600


def test_broker_timestamp_to_datetime_summer():
    """
    PU Prime Summer (EEST = UTC+3).
    Server clock 11:30:00 (represented by raw timestamp) corresponds to:
    - UTC: 08:30:00
    - Vietnam (UTC+7): 15:30:00
    """
    set_broker_utc_offset_hours(3)
    raw_server_ts = int(datetime(2026, 10, 13, 11, 30, 0, tzinfo=timezone.utc).timestamp())

    dt = broker_timestamp_to_datetime(raw_server_ts)
    assert dt.hour == 15
    assert dt.minute == 30
    assert dt.day == 13
    assert dt.month == 10
    assert dt.year == 2026


def test_broker_timestamp_to_datetime_winter():
    """
    PU Prime Winter (EET = UTC+2).
    Server clock 10:30:00 corresponds to:
    - UTC: 08:30:00
    - Vietnam (UTC+7): 15:30:00
    """
    set_broker_utc_offset_hours(2)
    raw_server_ts = int(datetime(2026, 1, 15, 10, 30, 0, tzinfo=timezone.utc).timestamp())

    dt = broker_timestamp_to_datetime(raw_server_ts)
    assert dt.hour == 15
    assert dt.minute == 30
    assert dt.day == 15
    assert dt.month == 1


def test_format_vietnam_time():
    """Test format_vietnam_time formats correctly from various inputs."""
    set_broker_utc_offset_hours(3)
    raw_server_ts = int(datetime(2026, 10, 13, 11, 15, 0, tzinfo=timezone.utc).timestamp())

    # Raw timestamp
    formatted = format_vietnam_time(raw_server_ts, fmt="%H:%M %d/%m")
    assert formatted == "15:15 13/10"

    formatted_full = format_vietnam_time(raw_server_ts, fmt="%H:%M:%S %d/%m/%Y")
    assert formatted_full == "15:15:00 13/10/2026"

    # Naive datetime
    naive_dt = datetime(2026, 10, 13, 15, 15)
    assert format_vietnam_time(naive_dt, "%H:%M %d/%m") == "15:15 13/10"

    # Aware UTC datetime
    utc_dt = datetime(2026, 10, 13, 8, 15, tzinfo=timezone.utc)
    assert format_vietnam_time(utc_dt, "%H:%M %d/%m") == "15:15 13/10"


def test_detect_and_update_broker_offset(monkeypatch):
    """Test dynamic detection of broker UTC offset from live tick timestamp."""
    # Suppose current real UTC time is 08:00:00
    fixed_now_utc = 1728806400.0  # 2024-10-13 08:00:00 UTC
    monkeypatch.setattr("time.time", lambda: fixed_now_utc)

    # Broker server is sending tick with server time 11:00:00 (+3 hours)
    tick_server_time = fixed_now_utc + 3 * 3600

    offset = detect_and_update_broker_offset(tick_server_time)
    assert offset == 3
    assert get_broker_utc_offset_hours() == 3

    # Broker switches to winter time (+2 hours)
    tick_winter = fixed_now_utc + 2 * 3600
    offset_winter = detect_and_update_broker_offset(tick_winter)
    assert offset_winter == 2
    assert get_broker_utc_offset_hours() == 2


def test_mapper_integrates_vietnam_timezone(monkeypatch):
    """Verify that MT5 mappers return domain objects with accurate Vietnam timezone datetimes."""
    fixed_now_utc = int(datetime(2026, 10, 13, 8, 45, 0, tzinfo=timezone.utc).timestamp())
    monkeypatch.setattr("time.time", lambda: float(fixed_now_utc))
    set_broker_utc_offset_hours(3)

    raw_ts = fixed_now_utc + 3 * 3600  # 11:45:00 server time

    # 1. Tick
    mock_tick = MagicMock()
    mock_tick.bid = 2750.50
    mock_tick.ask = 2750.70
    mock_tick.time = raw_ts
    tick_info = map_tick_info("XAUUSD", mock_tick)
    assert tick_info.time.hour == 15
    assert tick_info.time.minute == 45

    # 2. Deal
    mock_deal = MagicMock()
    mock_deal.ticket = 12345
    mock_deal.order = 67890
    mock_deal.position_id = 999
    mock_deal.symbol = "XAUUSD"
    mock_deal.type = 0
    mock_deal.entry = 1
    mock_deal.volume = 0.05
    mock_deal.price = 2750.0
    mock_deal.profit = 50.0
    mock_deal.swap = 0.0
    mock_deal.commission = -0.5
    mock_deal.time = raw_ts
    deal_info = map_deal_info(mock_deal)
    assert deal_info.time.hour == 15
    assert deal_info.time.minute == 45

    # 3. Position
    mock_pos = MagicMock()
    mock_pos.ticket = 11111
    mock_pos.symbol = "XAUUSD"
    mock_pos.type = 0
    mock_pos.volume = 0.01
    mock_pos.price_open = 2740.0
    mock_pos.price_current = 2750.0
    mock_pos.sl = 0.0
    mock_pos.tp = 0.0
    mock_pos.profit = 10.0
    mock_pos.swap = 0.0
    mock_pos.commission = 0.0
    mock_pos.magic = 0
    mock_pos.comment = ""
    mock_pos.time = raw_ts
    pos_info = map_position_info(mock_pos)
    assert pos_info.open_time.hour == 15
    assert pos_info.open_time.minute == 45

    # 4. Pending Order
    mock_order = MagicMock()
    mock_order.ticket = 22222
    mock_order.symbol = "XAUUSD"
    mock_order.type = 2  # BUY_LIMIT
    mock_order.volume_initial = 0.01
    mock_order.price_open = 2735.0
    mock_order.sl = 0.0
    mock_order.tp = 0.0
    mock_order.comment = ""
    mock_order.time_setup = raw_ts
    order_info = map_pending_order_info(mock_order)
    assert order_info.setup_time.hour == 15
    assert order_info.setup_time.minute == 45
