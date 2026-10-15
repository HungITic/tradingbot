"""
Signal Parser module — parses raw Telegram messages into structured domain objects.
"""

from __future__ import annotations

import re
from typing import Tuple

from app.signal.models import (
    MessageType,
    ManagementType,
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

PARSER_VERSION = "v1.1"

# Regex for detecting Action (BUY, SELL, MUA, BÁN, etc.)
ACTION_REGEX = re.compile(
    r"(?i)\b(?:LỆNH\s+|SIGNAL\s+|KÈO\s+)?(?:(?:LIMIT|STOP)[_\s]+)?(BUY|SELL|LONG|SHORT|MUA|BÁN|BAN)(?:[_\s]+(?:LIMIT|STOP|NOW|MARKET))?\b"
)

# Regex for Stop Loss
SL_REGEX = re.compile(
    r"(?i)\b(?:SL|STOP\s*LOSS|DỪNG\s*LỖ|S/L|STOPLOSS)\s*[:=\s–—\-@]\s*([\d.,]+)"
)

# Regex for Take Profit
TP_REGEX = re.compile(
    r"(?i)\b(?:TP\s*\d*|TAKE\s*PROFIT\s*\d*|CHỐT\s*LỜI\s*\d*|T/P\s*\d*)\s*[:=\s–—\-@]\s*([^\n\r]+)"
)

# Regex for detecting explicit Entry label
ENTRY_LABEL_REGEX = re.compile(
    r"(?i)\b(?:ENTRY|EP|VÀO\s*LỆNH|GIÁ\s*VÀO|VÙNG\s*VÀO|ZONE|AREA|OPEN|PRICE)\s*[:=\s–—\-@]\s*([\d.,]+(?:\s*(?:->|to|[-–—/~;])\s*[\d.,]+)?)"
)

# Range pattern (e.g. "4165 - 4171", "4165 – 4171", "4165/4171", "4165 to 4171")
RANGE_PATTERN = re.compile(
    r"([\d.,]+\s*(?:->|to|[-–—/~;])\s*[\d.,]+)",
    flags=re.IGNORECASE,
)

# Single price pattern (e.g. 4150.5, 2350, 4165)
SINGLE_PRICE_PATTERN = re.compile(
    r"(?:\bat\b|\b@\b|\bquanh\b|\bvùng\b|\bgiá\b)?\s*(\b\d{2,6}(?:[.,]\d+)?\b)(?!\s*(?:[%]|pips))",
    flags=re.IGNORECASE,
)

# Regex to detect symbol from any text (e.g. in management messages like "#XAU chốt bớt")
GENERIC_SYMBOL_REGEX = re.compile(
    r"(?i)(#[A-Za-z0-9_]+|\b(?:XAUUSD|EURUSD|GBPUSD|USDJPY|AUDUSD|USDCAD|USDCHF|NZDUSD|BTCUSD|ETHUSD|GOLD|VÀNG|VANG)\b|[A-Za-z]{3,6}/[A-Za-z]{3,6})"
)


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

    def _extract_entry_range(self, clean_text: str, lines: list[str]) -> str | None:
        """Extract entry price or range string using multiple resilient strategies."""
        # Strategy 1: Explicit entry label (e.g. "Entry: 4165 - 4171", "EP: 4165 - 4171", "Vào: 4165 - 4171")
        entry_match = ENTRY_LABEL_REGEX.search(clean_text)
        if entry_match:
            return entry_match.group(1).strip()

        # Strategy 2: Check lines excluding SL and TP lines
        non_sl_tp_lines = []
        for line in lines:
            line_str = line.strip()
            if not line_str:
                continue
            if SL_REGEX.search(line_str) or TP_REGEX.search(line_str):
                continue
            non_sl_tp_lines.append(line_str)

        # 2a: Check line containing Action or Symbol
        for idx, line in enumerate(non_sl_tp_lines):
            has_action = bool(ACTION_REGEX.search(line))
            has_sym = bool(GENERIC_SYMBOL_REGEX.search(line))

            if has_action or has_sym:
                # Remove action and symbol tokens from this line so remaining numbers are isolated
                cleaned_line = ACTION_REGEX.sub(" ", line)
                cleaned_line = GENERIC_SYMBOL_REGEX.sub(" ", cleaned_line)

                # Check for range pattern on this line first
                rm = RANGE_PATTERN.search(cleaned_line)
                if rm:
                    return rm.group(1).strip()

                # Check for single price on this line
                sp = SINGLE_PRICE_PATTERN.search(cleaned_line)
                if sp:
                    return sp.group(1).strip()

                # 2b: If line has Action/Symbol but no price/range, check the next line
                if idx + 1 < len(non_sl_tp_lines):
                    next_line = non_sl_tp_lines[idx + 1]
                    rm_next = RANGE_PATTERN.search(next_line)
                    if rm_next:
                        return rm_next.group(1).strip()
                    sp_next = SINGLE_PRICE_PATTERN.search(next_line)
                    if sp_next:
                        return sp_next.group(1).strip()

        # Strategy 3: Check all remaining non-SL/non-TP lines for any price range
        for line in non_sl_tp_lines:
            rm = RANGE_PATTERN.search(line)
            if rm:
                return rm.group(1).strip()

        return None

    def parse(self, raw_message: str) -> ParsedSignal:
        """Parse raw Telegram message text into structured ParsedSignal."""
        clean_text = re.sub(r"[*`]", "", raw_message).strip()
        lines = [line.strip() for line in clean_text.splitlines() if line.strip()]

        # Ambiguous, negated and conditional instructions require human review.
        if re.search(r"(?i)\b(?:chưa|không|đừng|khoan|nếu|chờ\s+xác\s+nhận|do\s+not|don't|if)\b", clean_text):
            return ParsedSignal(message_type=MessageType.UNKNOWN, raw_message=clean_text,
                                metadata={"reason": "Tin có phủ định hoặc điều kiện; không tự động thực thi"},
                                parser_version=PARSER_VERSION)

        # 1. Try matching NEW_ORDER pattern
        action = None
        action_match = ACTION_REGEX.search(clean_text)
        if action_match:
            action = normalize_action(action_match.group(1))

        symbol, raw_sym = self.detect_symbol(clean_text)

        # Parse SL first so distance to SL can determine min_entry and max_entry accurately
        sl_val = None
        sl_match = SL_REGEX.search(clean_text)
        if sl_match:
            sl_val = parse_price(sl_match.group(1))

        raw_range = None
        if action and symbol:
            raw_range = self._extract_entry_range(clean_text, lines)

        if action and symbol and raw_range:
            entry_min, entry_max = parse_price_range(raw_range, action=action, sl=sl_val)
            if entry_min is not None:
                # Parse TP
                tp_val = None
                tp_val_2 = None
                raw_tp_text = None
                tp_matches = list(TP_REGEX.finditer(clean_text))
                if tp_matches:
                    first_tp_str = tp_matches[0].group(1).strip()
                    # If TP contains text (e.g. 'Trên 200pips', 'open'), treat as descriptive text
                    if re.search(r"[a-zA-Z\u00C0-\u024F\u1E00-\u1EFF]", first_tp_str):
                        raw_tp_text = first_tp_str
                        tp_val = None
                    elif len(tp_matches) >= 2:
                        # Multiple TP lines (e.g. TP1: 4158 \n TP2: 4149)
                        tp_val = parse_price(tp_matches[0].group(1).strip())
                        tp_val_2 = parse_price(tp_matches[1].group(1).strip())
                    else:
                        # Single TP line: "4158 - 4149", "4158 / 4149", "4158, 4149", or "4158"
                        tp_parts = [p.strip() for p in re.split(r"[-–—/,]+", first_tp_str) if p.strip()]
                        if len(tp_parts) >= 2:
                            tp_val = parse_price(tp_parts[0])
                            tp_val_2 = parse_price(tp_parts[1])
                        elif len(tp_parts) == 1:
                            tp_val = parse_price(tp_parts[0])

                new_order = ParsedNewOrder(
                    action=action,
                    symbol=symbol,
                    raw_symbol=raw_sym or symbol,
                    entry_min=entry_min,
                    entry_max=entry_max,
                    stop_loss=sl_val,
                    take_profit=tp_val,
                    take_profit_2=tp_val_2,
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
            kinds = {a.action_type for a in actions}
            if ManagementType.CLOSE in kinds and len(kinds) > 1:
                return ParsedSignal(message_type=MessageType.UNKNOWN, raw_message=clean_text,
                    metadata={"reason": "Lệnh đóng toàn bộ xung đột với hành động quản lý khác; cần làm rõ"},
                    parser_version=PARSER_VERSION)
            stops = [a for a in actions if a.action_type in {ManagementType.MOVE_SL, ManagementType.BREAKEVEN}]
            if len(stops) > 1:
                return ParsedSignal(message_type=MessageType.UNKNOWN, raw_message=clean_text,
                    metadata={"reason": "Có nhiều đích SL; cần làm rõ"}, parser_version=PARSER_VERSION)
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
