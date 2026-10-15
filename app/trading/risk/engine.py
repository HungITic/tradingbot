"""
Risk Engine orchestrator — the gatekeeper between Signal Parser and Execution.
"""

from __future__ import annotations

import structlog

from app.signal.models import ParsedSignal
from app.trading.risk.calculator import LotCalculator
from app.trading.risk.models import RiskDecision, RiskResult, RiskStatus
from app.trading.risk.rules import (
    AbstractRiskRule,
    BotEnabledRule,
    BrokerConnectedRule,
    DailyLossRule,
    DuplicateSignalRule,
    MaxPositionsRule,
    RiskContext,
    StopLossRequiredRule,
    SymbolAllowedRule,
)

logger = structlog.get_logger(__name__)

DEFAULT_RISK_RULES: list[AbstractRiskRule] = [
    BotEnabledRule(),
    BrokerConnectedRule(),
    SymbolAllowedRule(),
    MaxPositionsRule(),
    DailyLossRule(),
    StopLossRequiredRule(),
    DuplicateSignalRule(),
]


class RiskEngine:
    """Evaluates all incoming trade signals against active risk policies."""

    def __init__(
        self,
        rules: list[AbstractRiskRule] | None = None,
        calculator: LotCalculator | None = None,
    ) -> None:
        self.rules = list(rules if rules is not None else DEFAULT_RISK_RULES)
        self.calculator = calculator or LotCalculator()

    def evaluate(self, signal: ParsedSignal, context: RiskContext) -> RiskDecision:
        """
        Evaluate signal against all rules.
        Fails fast upon the first REJECTED outcome.
        """
        results: list[RiskResult] = []
        is_approved = True
        rejection_reason = ""

        for rule in self.rules:
            result = rule.evaluate(signal, context)
            results.append(result)

            if result.status == RiskStatus.REJECTED:
                is_approved = False
                rejection_reason = f"[{result.rule_name}] {result.reason}"
                logger.warning(
                    "risk_check_rejected",
                    rule=rule.name,
                    reason=result.reason,
                    symbol=signal.symbol,
                )
                break

        lot = (
            self.calculator.calculate(signal, context.config)
            if is_approved and signal.message_type.value == "NEW_ORDER"
            else context.config.default_lot
        )

        if is_approved:
            logger.info("risk_check_approved", symbol=signal.symbol, lot=str(lot))

        return RiskDecision(
            approved=is_approved,
            results=results,
            rejection_reason=rejection_reason,
            calculated_lot=lot,
        )
