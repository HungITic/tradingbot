"""Commit recovery evidence before cancellations, closes, or opening orders."""
from sqlalchemy import case, cast, func, or_, update
from sqlalchemy.dialects.postgresql import JSONB

from app.config import database
from app.models.risk import RiskEvent
from app.models.signal import ParsedSignal


async def record_signal_recovery(signal_id, recovery):
    if signal_id is None:
        raise ValueError('Thiếu signal để lưu quyết định xử lý ngoại lệ')
    metadata = ParsedSignal.extra_metadata
    object_type = func.jsonb_typeof(metadata)
    document = case((object_type == 'object', metadata), else_=cast({}, JSONB))
    async with database.get_session() as session:
        result = await session.execute(update(ParsedSignal).where(
            ParsedSignal.signal_id == signal_id,
            or_(metadata.is_(None), object_type.in_(['object', 'null']))
        ).values(extra_metadata=document.op('||')(cast({'signal_recovery': recovery}, JSONB))))
        if result.rowcount != 1:
            raise ValueError('Không cập nhật được nhật ký xử lý ngoại lệ')
        session.add(RiskEvent(signal_id=signal_id, event_type='SIGNAL_RECOVERED',
                              rule_name='signal_recovery', details=recovery))
