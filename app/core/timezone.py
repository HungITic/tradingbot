"""
Timezone and timestamp utilities.

Handles conversion between MT5 broker server timestamps (e.g. PU Prime EET/EEST: UTC+3 summer, UTC+2 winter)
and local Vietnam time (Asia/Ho_Chi_Minh: UTC+7 / GMT+7).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Vietnam Timezone (ICT / UTC+7)
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# Default Broker Server UTC Offset in hours (PU Prime is EET/EEST: UTC+3 summer, UTC+2 winter)
DEFAULT_BROKER_UTC_OFFSET_HOURS = 3

_cached_broker_offset_hours: int | None = None


def get_broker_utc_offset_hours() -> int:
    """Return the active broker server UTC offset in hours."""
    global _cached_broker_offset_hours
    if _cached_broker_offset_hours is not None:
        return _cached_broker_offset_hours

    try:
        from app.config.settings import settings
        offset = getattr(settings.mt5, "broker_utc_offset_hours", None)
        if offset is not None:
            _cached_broker_offset_hours = int(offset)
            return _cached_broker_offset_hours
    except Exception:
        pass

    return DEFAULT_BROKER_UTC_OFFSET_HOURS


def set_broker_utc_offset_hours(offset_hours: int | None) -> None:
    """Explicitly set the broker server UTC offset in hours."""
    global _cached_broker_offset_hours
    if offset_hours is not None:
        try:
            _cached_broker_offset_hours = int(offset_hours)
        except (ValueError, TypeError):
            _cached_broker_offset_hours = DEFAULT_BROKER_UTC_OFFSET_HOURS
    else:
        _cached_broker_offset_hours = DEFAULT_BROKER_UTC_OFFSET_HOURS


def detect_and_update_broker_offset(tick_server_time: int | float | None) -> int:
    """
    Detect the broker server UTC offset dynamically using a live tick timestamp from MT5.

    In MT5, tick.time is the trade server clock expressed as Unix seconds.
    Comparing tick.time to current UTC time yields the broker's UTC offset.
    """
    if not tick_server_time or tick_server_time <= 0:
        return get_broker_utc_offset_hours()

    now_utc = time.time()
    diff = float(tick_server_time) - now_utc
    # Valid if tick is within reasonable recency (e.g. within 3 days over weekends)
    if abs(diff) < 86400 * 3:
        offset_hours = int(round(diff / 3600.0))
        if -12 <= offset_hours <= 14:
            set_broker_utc_offset_hours(offset_hours)
            return offset_hours

    return get_broker_utc_offset_hours()


def broker_timestamp_to_datetime(raw: int | float | datetime | None) -> datetime:
    """
    Convert an MT5 raw broker server timestamp to a timezone-aware Vietnam (UTC+7) datetime.

    Formula:
      real_utc_timestamp = broker_raw_timestamp - (broker_utc_offset_hours * 3600)
      vietnam_datetime = datetime.fromtimestamp(real_utc_timestamp, tz=timezone.utc).astimezone(VN_TZ)
    """
    if raw is None or raw == 0:
        return now_vietnam()

    if isinstance(raw, datetime):
        return to_vietnam_datetime(raw)

    try:
        raw_float = float(raw)
        if raw_float <= 0:
            return now_vietnam()
        offset_hours = get_broker_utc_offset_hours()
        real_utc_ts = raw_float - (offset_hours * 3600)
        return datetime.fromtimestamp(real_utc_ts, tz=timezone.utc).astimezone(VN_TZ)
    except Exception:
        return now_vietnam()


def now_vietnam() -> datetime:
    """Return current time in Vietnam timezone (UTC+7)."""
    return datetime.now(timezone.utc).astimezone(VN_TZ)


def to_vietnam_datetime(dt: datetime | int | float | None) -> datetime:
    """
    Convert any datetime or raw timestamp to a timezone-aware Vietnam (UTC+7) datetime.
    """
    if dt is None:
        return now_vietnam()

    if isinstance(dt, (int, float)):
        return broker_timestamp_to_datetime(dt)

    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            # If naive, treat as already in Vietnam timezone
            return dt.replace(tzinfo=VN_TZ)
        return dt.astimezone(VN_TZ)

    return now_vietnam()


def format_vietnam_time(dt: datetime | int | float | None, fmt: str = "%H:%M %d/%m") -> str:
    """Format any datetime or timestamp as a Vietnam time string."""
    vn_dt = to_vietnam_datetime(dt)
    return vn_dt.strftime(fmt)
