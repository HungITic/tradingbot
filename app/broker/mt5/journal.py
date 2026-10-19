"""Write-ahead journal for non-transactional MT5 commands. Never retry unknown outcomes."""
from dataclasses import asdict
from decimal import Decimal
from hashlib import blake2b
from uuid import uuid4

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from app.config import database
from app.config.settings import settings
from app.models.trade import BrokerCommand, Order
from app.broker.mt5.mapper import map_order_result


class UnresolvedBrokerCommand(RuntimeError):
    pass


def json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


async def send_journaled(client, api, request, *, opening=None):
    """Commit intent before send, then commit result and opening-order projection together.

    The account lock lives on a separate connection: intent/result commits must not
    release it. A crash leaves SUBMITTED, blocking further sends until reconciled.
    """
    account_key = f"{settings.mt5.server}:{settings.mt5.login}"
    lock_key = int.from_bytes(blake2b(account_key.encode(), digest_size=8).digest(), 'big', signed=True)
    command_id = uuid4().hex
    payload = {"mt5": json_value(request)}
    if opening is not None:
        payload["opening"] = json_value(asdict(opening))
    async with database.engine.connect() as lock_connection:
        await lock_connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
        if database.application_lock_pid is not None:
            owner_alive = await lock_connection.scalar(text("""SELECT EXISTS (
                SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND pid = :pid
                AND classid = 20261019 AND objid = 1 AND objsubid = 2 AND granted)"""),
                {"pid": database.application_lock_pid})
            if not owner_alive:
                raise UnresolvedBrokerCommand('Mất quyền thực thi của tiến trình; chưa gửi yêu cầu MT5')
        async with database.get_session() as session:
            unresolved = await session.scalar(select(BrokerCommand.id).where(
                BrokerCommand.account_key == account_key,
                BrokerCommand.status.in_(["SUBMITTED", "UNKNOWN"])).limit(1))
            if unresolved:
                raise UnresolvedBrokerCommand(f"Có yêu cầu MT5 chưa đối soát: {unresolved}; chưa gửi thêm lệnh")
            session.add(BrokerCommand(id=command_id, account_key=account_key,
                signal_id=opening.signal_id if opening else None, status="SUBMITTED", request=payload))
        # SUBMITTED is durable now. No exception/cancellation after this point permits replay.
        try:
            raw = await client.run_in_thread(api.order_send, request)
        except BaseException as exc:
            try:
                async with database.get_session() as session:
                    await session.execute(update(BrokerCommand).where(BrokerCommand.id == command_id)
                        .values(status="UNKNOWN", response={"error": type(exc).__name__}))
            except Exception:
                pass  # SUBMITTED remains durable and also blocks replay.
            raise
        result = map_order_result(raw)
        unknown = result.retcode in {-1, 0, 10012, 10031} or (result.success and opening and not result.ticket)
        status = "UNKNOWN" if unknown else "CONFIRMED" if result.success else "REJECTED"
        async with database.get_session() as session:
            await session.execute(update(BrokerCommand).where(BrokerCommand.id == command_id)
                .values(status=status, response=json_value(result.raw)))
            if result.success and opening and result.ticket:
                # This projection and the broker acknowledgement share one DB transaction.
                await session.execute(insert(Order).values(
                    signal_id=opening.signal_id, mt5_ticket=result.ticket,
                    mt5_order_type=request.get("type"), symbol=request["symbol"],
                    volume=result.volume or opening.volume, price=result.price or opening.price,
                    sl=opening.sl, tp=opening.tp, tp1=opening.tp1, tp2=opening.tp2,
                    comment=opening.comment, status="FILLED" if opening.order_type in {"BUY", "SELL"} else "PLACED",
                    mt5_retcode=result.retcode, mt5_comment=result.comment,
                ).on_conflict_do_nothing(index_elements=[Order.mt5_ticket]))
            elif result.success and request.get("action") == 8:  # TRADE_ACTION_REMOVE
                await session.execute(update(Order).where(Order.mt5_ticket == request["order"],
                    Order.status.in_(["PLACED", "PENDING"])).values(status="CANCELLED"))
        if unknown:
            raise UnresolvedBrokerCommand(f"Kết quả MT5 chưa xác định ({command_id}); cần đối soát, không gửi lại")
        return raw
