"""
Normalizer for symbols, actions, and price ranges.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from app.signal.models import OrderAction

DEFAULT_SYMBOL_MAP: dict[str, str] = {
    "#XAU": "XAUUSD",
    "XAU": "XAUUSD",
    "GOLD": "XAUUSD",
    "#GOLD": "XAUUSD",
    "VÀNG": "XAUUSD",
    "VANG": "XAUUSD",
    "#VÀNG": "XAUUSD",
    "#VANG": "XAUUSD",
    "XAUUSD": "XAUUSD",
    "XAUUSDM": "XAUUSDm",
    "#EUR": "EURUSD",
    "EURUSD": "EURUSD",
    "EURUSDM": "EURUSDm",
    "#GBP": "GBPUSD",
    "GBPUSD": "GBPUSD",
    "GBPUSDM": "GBPUSDm",
    "#BTC": "BTCUSD",
    "BTCUSD": "BTCUSD",
}

BUY_KEYWORDS = {"BUY", "LONG", "MUA"}
SELL_KEYWORDS = {"SELL", "SHORT", "BÁN", "BAN"}


class SymbolNormalizer:
    """Normalizes symbols and aliases, respecting account-specific suffixes."""

    def __init__(self, symbol_map: dict[str, str] | None = None, broker_suffix: str = "") -> None:
        self._map = dict(DEFAULT_SYMBOL_MAP)
        if symbol_map:
            self._map.update({k.upper(): v for k, v in symbol_map.items()})
        self._broker_suffix = broker_suffix

    def set_broker_suffix(self, suffix: str) -> None:
        self._broker_suffix = suffix

    def normalize(self, raw_symbol: str) -> str:
        """
        Normalize a raw symbol token (e.g., '#XAU' -> 'XAUUSD' or 'XAUUSDm').
        """
        clean = raw_symbol.strip().upper()
        # Direct lookup
        mapped = self._map.get(clean)
        if not mapped:
            # Strip leading hashtag
            if clean.startswith("#"):
                clean = clean[1:]
            mapped = self._map.get(clean, clean)

        # Append broker suffix if configured and not already present
        if self._broker_suffix and not mapped.endswith(self._broker_suffix):
            return f"{mapped}{self._broker_suffix}"
        return mapped


def normalize_action(raw_action: str) -> OrderAction | None:
    """Normalize action keyword to BUY or SELL."""
    token = raw_action.strip().upper()
    if token in BUY_KEYWORDS:
        return OrderAction.BUY
    if token in SELL_KEYWORDS:
        return OrderAction.SELL
    return None


def parse_price(raw_str: str) -> Decimal | None:
    """Safely parse a numeric string into Decimal."""
    if not raw_str:
        return None
    # If range string like '4146 - 4137' is passed, extract the first segment
    normalized = re.sub(r"[\u2013\u2014]", "-", raw_str.strip())
    first_part = normalized.split("-")[0].strip()
    # Remove commas, trailing punctuation
    clean = re.sub(r"[^\d.]", "", first_part)
    try:
        return Decimal(clean)
    except (InvalidOperation, ValueError):
        return None


def parse_price_range(
    range_str: str,
    action: Any = None,
    sl: Decimal | None = None,
) -> tuple[Decimal | None, Decimal | None]:
    """
    Parse price range formats like '4148 - 4160', '4148 – 4160', or single '4148.5'.
    Returns (entry_min, entry_max) according to the rule:
      min entry => giá xa SL hơn (điểm thăm dò / market order ban đầu)
      max entry => giá gần SL hơn (điểm limit order đón / mép sâu của vùng entry)
    """
    # Replace en-dash, em-dash with hyphen
    normalized = re.sub(r"[\u2013\u2014]", "-", range_str.strip())
    parts = [p.strip() for p in normalized.split("-") if p.strip()]

    if len(parts) == 2:
        p1 = parse_price(parts[0])
        p2 = parse_price(parts[1])
        if p1 is not None and p2 is not None:
            if p1 == p2:
                return (p1, p2)

            # 1. Nếu có SL: so sánh khoảng cách trực tiếp đến SL
            if sl is not None:
                d1 = abs(p1 - sl)
                d2 = abs(p2 - sl)
                if d1 >= d2:
                    return (p1, p2)  # p1 xa SL hơn -> min_entry=p1, max_entry=p2
                else:
                    return (p2, p1)  # p2 xa SL hơn -> min_entry=p2, max_entry=p1

            # 2. Nếu chưa có SL: suy luận từ action (BUY/SELL)
            act_str = action.value if hasattr(action, "value") else str(action or "").upper()
            if act_str == "BUY":
                # Kèo BUY: SL nằm dưới entry -> giá cao hơn xa SL hơn (min_entry), giá thấp hơn gần SL hơn (max_entry)
                return (max(p1, p2), min(p1, p2))
            elif act_str == "SELL":
                # Kèo SELL: SL nằm trên entry -> giá thấp hơn xa SL hơn (min_entry), giá cao hơn gần SL hơn (max_entry)
                return (min(p1, p2), max(p1, p2))

            return (p1, p2)
        return (p1, p2)
    elif len(parts) == 1:
        p = parse_price(parts[0])
        return (p, p)

    return (None, None)
