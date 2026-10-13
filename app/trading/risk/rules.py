"""
Individual risk verification rules.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.broker.base import AccountInfo, PositionInfo
from app.signal.models import ParsedSignal
from app.trading.risk.models import RiskConfig, RiskResult, RiskStatus


@dataclass
class RiskContext:
    """Snapshot of current system state passed to risk rules."""

    config: RiskConfig
    account: AccountInfo
    open_positions: list[PositionInfo] = field(default_factory=list)
    today_realized_pnl: Decimal = Decimal("0.0")
    today_starting_balance: Decimal = Decimal("500.0")
    recent_signals: list[dict[str, Any]] = field(default_factory=list)
    broker_connected: bool = True


class AbstractRiskRule(ABC):
    """Abstract base class for all risk rules."""

    name: str

    @abstractmethod
    def evaluate(self, signal: ParsedSignal, context: RiskContext) -> RiskResult:
        ...


class BotEnabledRule(AbstractRiskRule):
    name = "bot_enabled"

    def evaluate(self, signal: ParsedSignal, context: RiskContext) -> RiskResult:
        if not context.config.enabled:
            return RiskResult(
                rule_name=self.name,
                status=RiskStatus.REJECTED,
                reason="Bot trading is currently paused or disabled",
            )
        return RiskResult(rule_name=self.name, status=RiskStatus.APPROVED)


class BrokerConnectedRule(AbstractRiskRule):
    name = "broker_connected"

    def evaluate(self, signal: ParsedSignal, context: RiskContext) -> RiskResult:
        if not context.broker_connected:
            return RiskResult(
                rule_name=self.name,
                status=RiskStatus.REJECTED,
                reason="Broker terminal MT5 is not connected",
            )
        return RiskResult(rule_name=self.name, status=RiskStatus.APPROVED)


class SymbolAllowedRule(AbstractRiskRule):
    name = "symbol_allowed"

    def evaluate(self, signal: ParsedSignal, context: RiskContext) -> RiskResult:
        sym = signal.symbol
        if not sym:
            return RiskResult(
                rule_name=self.name,
                status=RiskStatus.REJECTED,
                reason="Signal symbol is unspecified",
            )

        allowed = [s.upper() for s in context.config.allowed_symbols]
        sym_upper = sym.upper()
        if sym_upper not in allowed:
            known_suffixes = (".S", ".RAW", ".M", ".C", "M", "C", "+", "PRO", "_I")
            def _strip_suffix(s: str) -> str:
                for suf in known_suffixes:
                    if s.endswith(suf):
                        return s[:-len(suf)]
                return s

            sym_base = _strip_suffix(sym_upper)
            allowed_bases = [_strip_suffix(a) for a in allowed]
            if sym_base not in allowed_bases and not any(sym_upper.startswith(a) for a in allowed):
                return RiskResult(
                    rule_name=self.name,
                    status=RiskStatus.REJECTED,
                    reason=f"Symbol '{sym}' is not in allowed list: {allowed}",
                )
        return RiskResult(rule_name=self.name, status=RiskStatus.APPROVED)


class MaxPositionsRule(AbstractRiskRule):
    name = "max_positions"

    def evaluate(self, signal: ParsedSignal, context: RiskContext) -> RiskResult:
        # Management actions don't open new positions, only check on NEW_ORDER
        if signal.message_type.value != "NEW_ORDER":
            return RiskResult(rule_name=self.name, status=RiskStatus.APPROVED)

        current_count = len(context.open_positions)
        if current_count >= context.config.max_open_positions:
            return RiskResult(
                rule_name=self.name,
                status=RiskStatus.REJECTED,
                reason=f"Max open positions reached ({current_count}/{context.config.max_open_positions})",
                details={"current": current_count, "limit": context.config.max_open_positions},
            )
        return RiskResult(rule_name=self.name, status=RiskStatus.APPROVED)


class DailyLossRule(AbstractRiskRule):
    name = "daily_loss_limit"

    def evaluate(self, signal: ParsedSignal, context: RiskContext) -> RiskResult:
        if signal.message_type.value != "NEW_ORDER":
            return RiskResult(rule_name=self.name, status=RiskStatus.APPROVED)

        base_balance = context.today_starting_balance or context.account.balance
        if base_balance <= 0:
            return RiskResult(rule_name=self.name, status=RiskStatus.APPROVED)

        # If today realized PnL is negative and exceeds loss percent
        if context.today_realized_pnl < 0:
            loss_percent = abs(context.today_realized_pnl) / base_balance * Decimal("100.0")
            if loss_percent >= context.config.max_daily_loss_percent:
                return RiskResult(
                    rule_name=self.name,
                    status=RiskStatus.REJECTED,
                    reason=f"Daily loss limit reached: -{loss_percent:.1f}% >= {context.config.max_daily_loss_percent}%",
                    details={"loss_pct": float(loss_percent), "limit_pct": float(context.config.max_daily_loss_percent)},
                )
        return RiskResult(rule_name=self.name, status=RiskStatus.APPROVED)


class StopLossRequiredRule(AbstractRiskRule):
    name = "stop_loss_required"

    def evaluate(self, signal: ParsedSignal, context: RiskContext) -> RiskResult:
        if signal.message_type.value != "NEW_ORDER" or not signal.new_order:
            return RiskResult(rule_name=self.name, status=RiskStatus.APPROVED)

        if context.config.require_sl and signal.new_order.stop_loss is None:
            return RiskResult(
                rule_name=self.name,
                status=RiskStatus.REJECTED,
                reason="Stop Loss is strictly required by risk policy",
            )
        return RiskResult(rule_name=self.name, status=RiskStatus.APPROVED)


class DuplicateSignalRule(AbstractRiskRule):
    name = "duplicate_signal"

    def evaluate(self, signal: ParsedSignal, context: RiskContext) -> RiskResult:
        if signal.message_type.value != "NEW_ORDER" or not signal.new_order:
            return RiskResult(rule_name=self.name, status=RiskStatus.APPROVED)
            
        if getattr(signal, "is_reentry", False):
            return RiskResult(rule_name=self.name, status=RiskStatus.APPROVED, reason="Bypassed for reentry signal")

        now = time.time()
        sym = signal.symbol
        act = signal.new_order.action.value

        for item in context.recent_signals:
            if item.get("symbol") == sym and item.get("action") == act:
                elapsed = now - item.get("timestamp", 0)
                if elapsed < context.config.duplicate_timeout_seconds:
                    return RiskResult(
                        rule_name=self.name,
                        status=RiskStatus.REJECTED,
                        reason=f"Duplicate signal detected ({act} {sym} received {elapsed:.0f}s ago; timeout={context.config.duplicate_timeout_seconds}s)",
                    )
        return RiskResult(rule_name=self.name, status=RiskStatus.APPROVED)
