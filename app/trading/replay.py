"""Reserve explicit retries only when the audit proves no trading effects occurred."""
from sqlalchemy import select, exists, or_

from app.models.signal import Signal, SignalSource, SignalReceipt
from app.models.trade import Order, BrokerCommand, ManagementAction
from app.models.risk import RiskEvent


async def reclaim_unexecuted_message(session, chat_id, message_id, raw_text, context):
    receipt = await session.scalar(select(SignalReceipt).where(
        SignalReceipt.chat_id == chat_id, SignalReceipt.message_id == message_id).with_for_update())
    if receipt is None:
        return False
    attempts = (await session.execute(select(Signal.id, Signal.status)
        .join(SignalSource, Signal.source_id == SignalSource.id)
        .where(SignalSource.chat_id == chat_id, Signal.telegram_message_id == message_id)
        .order_by(Signal.id.desc()))).all()
    if not attempts or any(status not in {'UNKNOWN', 'REJECTED', 'SKIPPED', 'EXPIRED'}
                           for _, status in attempts):
        return False
    latest_id = attempts[0][0]
    # A committed reservation without a subsequent audit row is an interrupted retry.
    # Do not let another process reuse the old terminal status in that window.
    previous = (receipt.context or {}).get('retry_after_signal_id')
    if previous is not None and latest_id <= previous:
        return False
    ids = [sid for sid, _ in attempts]
    effects = or_(
        exists().where(Order.signal_id.in_(ids)),
        exists().where(BrokerCommand.signal_id.in_(ids)),
        exists().where(ManagementAction.signal_id.in_(ids)),
        exists().where(BrokerCommand.status.in_(['SUBMITTED', 'UNKNOWN'])),
        exists().where(RiskEvent.event_type == 'LIMIT_INVALIDATED',
            RiskEvent.details['trigger_chat_id'].astext == str(chat_id),
            RiskEvent.details['trigger_message_id'].astext == str(message_id)))
    if await session.scalar(select(effects)):
        return False
    receipt.raw_message = raw_text
    receipt.context = {**context, 'retry_after_signal_id': latest_id}
    return True
