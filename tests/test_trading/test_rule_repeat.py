"""One persisted rule defines matching, percentage and repeat permission."""
from contextlib import asynccontextmanager
from decimal import Decimal as D
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.signal.models import ManagementType
from app.signal.rules import ManagementRuleDefinition, ManagementRuleEngine
from app.trading.service import TradingService


@pytest.fixture
def rule_store(monkeypatch):
    rows = []
    session = AsyncMock()
    session.add = MagicMock(side_effect=rows.append)
    result = MagicMock()
    result.scalar_one_or_none.side_effect = lambda: rows[0] if rows else None
    result.scalars.return_value.all.side_effect = lambda: rows
    session.execute.return_value = result
    @asynccontextmanager
    async def db():
        yield session
    monkeypatch.setattr('app.config.database.get_session', db)
    return rows, session


@pytest.mark.asyncio
@pytest.mark.parametrize('allow', [True, False])
async def test_save_reload_rule_keeps_percentage_and_repeat_together(rule_store, allow):
    rows, _ = rule_store
    svc = TradingService(broker=AsyncMock())
    assert await svc.add_or_update_management_rule('chốt bớt entry xấu', 'PARTIAL_CLOSE', D('30'), allow_repeat=allow)
    assert rows[0].parameters == {'percentage': 30, 'percentage_explicit': True, 'allow_repeat': allow}
    restarted = TradingService(broker=AsyncMock())
    assert await restarted.load_management_rules_from_db() == 1
    action = restarted.parser.parse('XAUUSD chốt bớt entry xấu').management_actions[0]
    assert action.allow_repeat is allow
    assert restarted.partial_percentage(action) == D('30')
    # Rule overrides both the global switch and legacy whitelist.
    restarted.risk_config.allow_multiple_partial_closes = not allow
    restarted.risk_config.repeat_partial_close_keywords = ['chốt bớt entry xấu']
    assert restarted.repeat_partial_allowed('XAUUSD chốt bớt entry xấu', action) is allow
    explicit = restarted.parser.parse('XAUUSD chốt bớt entry xấu 40%').management_actions[0]
    assert restarted.partial_percentage(explicit) == D('40')


@pytest.mark.asyncio
async def test_edit_percentage_without_repeat_preserves_previous_permission(rule_store):
    svc = TradingService(broker=AsyncMock())
    assert await svc.add_or_update_management_rule('chốt bớt', 'PARTIAL_CLOSE', D('30'), allow_repeat=True)
    assert await svc.add_or_update_management_rule('chốt bớt', 'PARTIAL_CLOSE', D('40'))
    action = svc.parser.parse('chốt bớt').management_actions[0]
    assert action.allow_repeat is True
    assert svc.partial_percentage(action) == D('40')


@pytest.mark.asyncio
@pytest.mark.parametrize('action,percentage,allow', [
    ('CLOSE', None, True), ('PARTIAL_CLOSE', D('101'), True),
    ('PARTIAL_CLOSE', D('NaN'), True), ('PARTIAL_CLOSE', D('30'), 'false'),
])
async def test_invalid_rule_never_changes_database_or_parser(rule_store, action, percentage, allow):
    rows, session = rule_store
    svc = TradingService(broker=AsyncMock())
    before = svc.get_management_rules()
    assert not await svc.add_or_update_management_rule('custom', action, percentage, allow_repeat=allow)
    assert not rows and svc.get_management_rules() == before
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_commit_does_not_publish_new_repeat_rule(rule_store, monkeypatch):
    _, session = rule_store
    svc = TradingService(broker=AsyncMock())
    @asynccontextmanager
    async def failed_db():
        yield session
        raise RuntimeError('commit failed')
    monkeypatch.setattr('app.config.database.get_session', failed_db)
    assert not await svc.add_or_update_management_rule('custom', 'PARTIAL_CLOSE', D('30'), allow_repeat=True)
    assert not any(rule.keyword == 'custom' for rule in svc.get_management_rules())


def test_conflicting_equivalent_triggers_produce_one_denied_repeat_action():
    rules = [ManagementRuleDefinition('chốt tiếp', ManagementType.PARTIAL_CLOSE, {'allow_repeat': True}),
             ManagementRuleDefinition('giảm bớt', ManagementType.PARTIAL_CLOSE, {'allow_repeat': False})]
    for ordered in (rules, list(reversed(rules))):
        actions = ManagementRuleEngine(ordered).match_actions('chốt tiếp và giảm bớt')
        assert len(actions) == 1 and actions[0].allow_repeat is False


def test_longer_keyword_repeat_setting_does_not_leak_to_generic_keyword():
    svc = TradingService(broker=AsyncMock())
    svc.parser.rule_engine.add_or_update_rule('chốt bớt entry xấu', ManagementType.PARTIAL_CLOSE,
                                             {'allow_repeat': True})
    assert svc.parser.parse('chốt bớt entry xấu').management_actions[0].allow_repeat is True
    assert svc.parser.parse('chốt bớt').management_actions[0].allow_repeat is None
