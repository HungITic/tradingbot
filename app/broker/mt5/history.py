"""Read history before mt5_mac's namedtuple conversion drops position_id."""
from datetime import datetime


def read_history_deals(api, start, end, *, position_id=None):
    if getattr(api, "__name__", "") == "mt5_mac":
        # mt5_mac 0.3 declares `position`, but the official agent sends
        # `position_id`. Its MqlDeal construction silently discards that field.
        # Keep the original dictionaries so our mapper sees the broker identity.
        backend = getattr(api, "_backend", None)
        if backend is None:
            raise RuntimeError("MT5 bridge is not connected")
        start_ts = int(start.timestamp()) if isinstance(start, datetime) else int(start)
        end_ts = int(end.timestamp()) if isinstance(end, datetime) else int(end)
        result = backend._send({"action": "history_deals", "from": 0 if position_id else start_ts, "to": end_ts})
        return result.get("deals") if result else None
    if position_id is not None:
        return api.history_deals_get(position=position_id)
    return api.history_deals_get(start, end)
