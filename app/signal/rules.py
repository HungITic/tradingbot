"""
Dynamic management rules and keyword pattern matching.

Supports configurable rules, customizable percentages, and dynamic keyword additions.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from app.signal.models import ManagementType, ParsedManagementAction


class ManagementRuleDefinition:
    """Represents a configurable keyword rule."""

    def __init__(
        self,
        keyword: str,
        action_type: ManagementType,
        default_params: dict[str, Any] | None = None,
        priority: int = 0,
    ) -> None:
        self.keyword = keyword.lower().strip()
        self.action_type = action_type
        self.default_params = default_params or {}
        self.priority = priority

    def __repr__(self) -> str:
        return f"<Rule '{self.keyword}' -> {self.action_type}>"


DEFAULT_RULES: list[ManagementRuleDefinition] = [
    # Partial close (chốt bớt / chốt 1 phần)
    ManagementRuleDefinition("chốt bớt 1 phần", ManagementType.PARTIAL_CLOSE, {"percentage": Decimal("50")}, priority=12),
    ManagementRuleDefinition("chốt 1 phần", ManagementType.PARTIAL_CLOSE, {"percentage": Decimal("50")}, priority=11),
    ManagementRuleDefinition("chốt lãi 1 phần", ManagementType.PARTIAL_CLOSE, {"percentage": Decimal("50")}, priority=11),
    ManagementRuleDefinition("chốt bớt lãi", ManagementType.PARTIAL_CLOSE, {"percentage": Decimal("50")}, priority=11),
    ManagementRuleDefinition("chốt bớt", ManagementType.PARTIAL_CLOSE, {"percentage": Decimal("50")}, priority=10),

    # Full close (chốt hết / chốt all / đóng lệnh)
    ManagementRuleDefinition("chốt hết", ManagementType.CLOSE, {"percentage": Decimal("100")}, priority=10),
    ManagementRuleDefinition("chốt all", ManagementType.CLOSE, {"percentage": Decimal("100")}, priority=10),
    ManagementRuleDefinition("chốt nha", ManagementType.CLOSE, {"percentage": Decimal("100")}, priority=9),
    ManagementRuleDefinition("chốt nhé", ManagementType.CLOSE, {"percentage": Decimal("100")}, priority=9),
    ManagementRuleDefinition("chốt đi", ManagementType.CLOSE, {"percentage": Decimal("100")}, priority=9),
    ManagementRuleDefinition("chốt lời", ManagementType.CLOSE, {"percentage": Decimal("100")}, priority=8),
    ManagementRuleDefinition("đóng lệnh", ManagementType.CLOSE, {"percentage": Decimal("100")}, priority=8),
    ManagementRuleDefinition("đóng hết", ManagementType.CLOSE, {"percentage": Decimal("100")}, priority=8),
    ManagementRuleDefinition("đóng hoà", ManagementType.CLOSE, {"percentage": Decimal("100")}, priority=8),
    ManagementRuleDefinition("đóng hòa", ManagementType.CLOSE, {"percentage": Decimal("100")}, priority=8),
    ManagementRuleDefinition("đóng âm", ManagementType.CLOSE, {"percentage": Decimal("100")}, priority=8),
    ManagementRuleDefinition("đóng buy", ManagementType.CLOSE, {"percentage": Decimal("100")}, priority=8),
    ManagementRuleDefinition("đóng sell", ManagementType.CLOSE, {"percentage": Decimal("100")}, priority=8),
    ManagementRuleDefinition("cắt hết", ManagementType.CLOSE, {"percentage": Decimal("100")}, priority=8),
    ManagementRuleDefinition("dừng", ManagementType.CLOSE, {"percentage": Decimal("100")}, priority=1),

    # Move SL / Breakeven
    ManagementRuleDefinition("dời sl về entry", ManagementType.MOVE_SL, {"target": "ENTRY"}, priority=10),
    ManagementRuleDefinition("dời sl hoà", ManagementType.BREAKEVEN, {"target": "ENTRY"}, priority=10),
    ManagementRuleDefinition("dời sl hòa", ManagementType.BREAKEVEN, {"target": "ENTRY"}, priority=10),
    ManagementRuleDefinition("dời sl huề", ManagementType.BREAKEVEN, {"target": "ENTRY"}, priority=10),
    ManagementRuleDefinition("dịch stop loss", ManagementType.MOVE_SL, {"target": "ENTRY"}, priority=9),
    ManagementRuleDefinition("dời stop loss", ManagementType.MOVE_SL, {"target": "ENTRY"}, priority=9),
    ManagementRuleDefinition("dịch stoploss", ManagementType.MOVE_SL, {"target": "ENTRY"}, priority=8),
    ManagementRuleDefinition("dời stoploss", ManagementType.MOVE_SL, {"target": "ENTRY"}, priority=8),
    ManagementRuleDefinition("dịch sl", ManagementType.MOVE_SL, {"target": "ENTRY"}, priority=8),
    ManagementRuleDefinition("dời sl", ManagementType.MOVE_SL, {"target": "ENTRY"}, priority=8),
    ManagementRuleDefinition("kéo sl dương", ManagementType.MOVE_SL, {"target": "ENTRY"}, priority=8),
    ManagementRuleDefinition("kéo sl", ManagementType.MOVE_SL, {"target": "ENTRY"}, priority=8),
    ManagementRuleDefinition("hòa vốn", ManagementType.BREAKEVEN, {"target": "ENTRY"}, priority=8),
    ManagementRuleDefinition("hoà vốn", ManagementType.BREAKEVEN, {"target": "ENTRY"}, priority=8),
    ManagementRuleDefinition("breakeven", ManagementType.BREAKEVEN, {"target": "ENTRY"}, priority=8),

    # Re-entry
    ManagementRuleDefinition("vào lại", ManagementType.REENTRY, {}, priority=8),
    ManagementRuleDefinition("vòng 2", ManagementType.REENTRY, {}, priority=8),
    ManagementRuleDefinition("re-enter", ManagementType.REENTRY, {}, priority=8),
]


class ManagementRuleEngine:
    """Matches text against registered management rules with conflict priority."""

    def __init__(self, rules: list[ManagementRuleDefinition] | None = None) -> None:
        self._rules = list(rules if rules is not None else DEFAULT_RULES)
        self._sort_rules()

    def _sort_rules(self) -> None:
        """Sort rules by: longer keyword first, then priority DESC."""
        self._rules.sort(key=lambda r: (-len(r.keyword), -r.priority))

    def update_rules(self, rules: list[ManagementRuleDefinition]) -> None:
        """Replace active rules (e.g., reloaded from database)."""
        self._rules = list(rules)
        self._sort_rules()

    def add_or_update_rule(
        self,
        keyword: str,
        action_type: ManagementType,
        params: dict[str, Any] | None = None,
        priority: int = 5,
    ) -> None:
        """Dynamically add or update a rule via Telegram Bot."""
        clean_kw = keyword.lower().strip()
        # Remove existing rule with same keyword
        self._rules = [r for r in self._rules if r.keyword != clean_kw]
        self._rules.append(ManagementRuleDefinition(clean_kw, action_type, params, priority))
        self._sort_rules()

    def remove_rule(self, keyword: str) -> bool:
        """Remove a rule by keyword."""
        clean_kw = keyword.lower().strip()
        before_len = len(self._rules)
        self._rules = [r for r in self._rules if r.keyword != clean_kw]
        return len(self._rules) < before_len

    def match_actions(self, text: str) -> list[ParsedManagementAction]:
        """
        Extract all triggered management actions from message text.
        Handles combos like: '#XAU Kèo chạy rồi +120pips chốt bớt dịch stoploss nha'
        """
        lower_text = text.lower()
        actions: list[ParsedManagementAction] = []
        matched_spans: list[tuple[int, int]] = []

        # Check for explicit percentage in text (e.g., 'chốt bớt 30%')
        explicit_pct: Decimal | None = None
        pct_match = re.search(r"(\d{1,3})\s*%", lower_text)
        if pct_match:
            try:
                explicit_pct = Decimal(pct_match.group(1))
            except Exception:
                pass

        for rule in self._rules:
            pattern = re.escape(rule.keyword)
            for m in re.finditer(pattern, lower_text):
                start, end = m.span()
                # Check for overlap with already matched longer keywords
                overlaps = any(not (end <= s or start >= e) for s, e in matched_spans)
                if overlaps:
                    continue

                matched_spans.append((start, end))

                # Build action
                pct = explicit_pct or rule.default_params.get("percentage")
                target = rule.default_params.get("target")

                action = ParsedManagementAction(
                    action_type=rule.action_type,
                    percentage=pct,
                    target=target,
                    keyword=rule.keyword,
                )
                actions.append(action)

        return actions
