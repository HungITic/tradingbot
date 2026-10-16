"""Calendar boundaries for history filters, always in Vietnam time."""
from datetime import timedelta

from app.core.timezone import now_vietnam


def history_period(value="today"):
    now = now_vietnam()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if value == "today":
        return midnight, now, "Hôm nay"
    if value == "yesterday":
        return midnight - timedelta(days=1), midnight, "Hôm qua"
    if value == "month":
        return midnight.replace(day=1), now, "Tháng này"
    days = max(1, int(value))
    return now - timedelta(days=days), now, f"{days} ngày gần nhất"
