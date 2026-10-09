"""
Signal Parser module — parses raw Telegram messages into structured domain objects.
"""

from __future__ import annotations

import re
from typing import Tuple

from app.signal.models import (
    MessageType,
    OrderAction,
    ParsedNewOrder,
    ParsedSignal,
)
from app.signal.normalizer import (
    SymbolNormalizer,
    normalize_action,
    parse_price,
    parse_price_range,
)
from app.signal.rules import ManagementRuleEngine

PARSER_VERSION = "v1.0"

# Regex for New Order Header
# Example: "Sell #XAU 4148 - 4160", "Buy XAUUSD 4150.5", "BUY #EUR 1.0850 - 1.0870"
ORDER_HEADER_REGEX = re.compile(
    r"(?i)\b(BUY|SELL|LONG|SHORT|MUA|BÁN)\s+(#[A-Za-z0-9_]+|[A-Za-z]{3,10})(?:\s+(?:at|@))?\s+([\d.,]+(?:\s*[-–—]\s*[\d.,]+)?)"
)

# Regex for Stop Loss
SL_REGEX = re.compile(r"(?i)\b(?:SL|STOP\s*LOSS|DỪNG\s*LỖ)\s*[:=\s]\s*([\d.,]+)")

# Regex for Take Profit
TP_REGEX = re.compile(r"(?i)\b(?:TP\s*\d*|TAKE\s*PROFIT|CHỐT\s*LỜI)\s*[:=\s]\s*([^\n\r]+)")

# Regex to detect symbol from any text (e.g. in management messages like "#XAU chốt bớt")
GENERIC_SYMBOL_REGEX = re.compile(r"(?i)(#[A-Za-z0-9_]+|\b(?:XAUUSD|EURUSD|GBPUSD|BTCUSD|GOLD)\b)")


class SignalParser:
    """End-to-end signal parser pipeline."""

    def __init__(
        self,
        normalizer: SymbolNormalizer | None = None,
        rule_engine: ManagementRuleEngine | None = None,
    ) -> None:
        self.normalizer = normalizer or SymbolNormalizer()
        self.rule_engine = rule_engine or ManagementRuleEngine()

    def detect_symbol(self, text: str) -> Tuple[str | None, str | None]:
        """
        Extract and normalize symbol token from text.
        Returns (normalized_symbol, raw_symbol).
        """
        match = GENERIC_SYMBOL_REGEX.search(text)
        if match:
            raw = match.group(1)
            return self.normalizer.normalize(raw), raw
        return None, None

    def parse(self, raw_message: str) -> ParsedSignal:
        """Parse raw Telegram message text into structured ParsedSignal."""
        clean_text = raw_message.strip()

        # 1. Try matching NEW_ORDER pattern
        order_match = ORDER_HEADER_REGEX.search(clean_text)
        if order_match:
            raw_action = order_match.group(1)
            raw_sym = order_match.group(2)
            raw_range = order_match.group(3)

            action = normalize_action(raw_action)
            symbol = self.normalizer.normalize(raw_sym)
            entry_min, entry_max = parse_price_range(raw_range)

            # Parse SL
            sl_val = None
            sl_match = SL_REGEX.search(clean_text)
            if sl_match:
                sl_val = parse_price(sl_match.group(1))

            # Parse TP
            tp_val = None
            raw_tp_text = None
            tp_match = TP_REGEX.search(clean_text)
            if tp_match:
                tp_str = tp_match.group(1).strip()
                # If TP contains text (e.g. 'Trên 200pips', 'open'), treat as descriptive text
                if re.search(r"[a-zA-Z\u00C0-\u024F\u1E00-\u1EFF]", tp_str):
                    raw_tp_text = tp_str
                    tp_val = None
                else:
                    tp_val = parse_price(tp_str)

            new_order = ParsedNewOrder(
                action=action or OrderAction.BUY,
                symbol=symbol,
                raw_symbol=raw_sym,
                entry_min=entry_min,
                entry_max=entry_max,
                stop_loss=sl_val,
                take_profit=tp_val,
                raw_tp_text=raw_tp_text,
            )

            return ParsedSignal(
                message_type=MessageType.NEW_ORDER,
                raw_message=clean_text,
                symbol=symbol,
                new_order=new_order,
                parser_version=PARSER_VERSION,
            )

        # 2. Try matching POSITION_MANAGEMENT keywords
        actions = self.rule_engine.match_actions(clean_text)
        if actions:
            symbol, _ = self.detect_symbol(clean_text)
            return ParsedSignal(
                message_type=MessageType.POSITION_MANAGEMENT,
                raw_message=clean_text,
                symbol=symbol,
                management_actions=actions,
                parser_version=PARSER_VERSION,
            )

        # 3. UNKNOWN message — preserved for AI replay (Phase 10)
        return ParsedSignal(
            message_type=MessageType.UNKNOWN,
            raw_message=clean_text,
            symbol=self.detect_symbol(clean_text)[0],
            parser_version=PARSER_VERSION,
        )
