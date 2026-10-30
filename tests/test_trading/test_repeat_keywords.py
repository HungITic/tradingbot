from contextlib import asynccontextmanager
import unicodedata
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.trading.risk import RiskConfig
from app.trading.service import TradingService


@pytest.mark.parametrize('keywords,text,expected', [
    (' CHỐT BỚT ENTRY XẤU ', 'XAUUSD CHỐT   BỚT\nENTRY XẤU nhé', True),
    (['entry xấu'], unicodedata.normalize('NFD', 'XAUUSD chốt bớt entry xấu'), True),
    (['chốt'], 'XAUUSD chốtt', False),
    (['xấu'], 'XAUUSD chốt bớt', False),
    (['chốt.*'], 'chốt bớt entry xấu', False),
    (['chốt + xấu'], 'XAUUSD chốt + xấu!', True),
    ('[]', 'XAUUSD chốt bớt entry xấu', False),
    ('', 'XAUUSD chốt bớt', False),
])
def test_literal_keyword_matching_normalizes_text_without_regex_or_subword_matches(keywords, text, expected):
    service = TradingService(broker=AsyncMock(), risk_config=RiskConfig(repeat_partial_close_keywords=keywords))
    assert bool(service.repeat_partial_allowed(text)) == expected


def test_keyword_input_deduplicates_and_rejects_invalid_items():
    config = RiskConfig().with_updates({'repeat_partial_close_keywords': 'Chốt bớt, CHỐT BỚT\nentry xấu,  '})
    assert config.repeat_partial_close_keywords == ['chốt bớt', 'entry xấu']
    assert config.with_updates({'repeat_partial_close_keywords': '[]'}).repeat_partial_close_keywords == []
    with pytest.raises(ValidationError):
        config.with_updates({'repeat_partial_close_keywords': [123]})


@pytest.mark.asyncio
async def test_keyword_config_persists_and_loads_in_a_fresh_service(monkeypatch):
    saved = []
    session = AsyncMock()
    session.add = MagicMock(side_effect=saved.append)
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.scalars.return_value.all.side_effect = lambda: saved
    session.execute.return_value = result
    @asynccontextmanager
    async def db():
        yield session
    monkeypatch.setattr('app.config.database.get_session', db)
    service = TradingService(broker=AsyncMock())
    assert await service.update_config_key('repeat_partial_close_keywords', 'chốt bớt entry xấu')
    management = next(row for row in saved if row.key == 'position_management')
    assert management.value['repeat_partial_close_keywords'] == ['chốt bớt entry xấu']
    restarted = TradingService(broker=AsyncMock())
    await restarted.load_risk_config_from_db()
    assert restarted.repeat_partial_allowed('XAUUSD chốt bớt entry xấu')
    assert not restarted.repeat_partial_allowed('XAUUSD chốt bớt')
