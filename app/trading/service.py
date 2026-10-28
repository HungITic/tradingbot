"""
Trading Service — coordinates signals, risk evaluation, execution, and admin commands.
"""

from __future__ import annotations

import time
import asyncio
from datetime import datetime, timezone
from dataclasses import replace
from html import escape
from decimal import Decimal
from typing import Any, Callable, Coroutine

import structlog

from app.broker.base import (
    AccountInfo,
    BrokerAdapter,
    ClosedPositionInfo,
    DealInfo,
    OrderRequest,
    OrderResult,
    PartialCloseDealInfo,
    PositionInfo,
    TickInfo,
)
from app.broker.mt5.executor import MT5Executor
from app.signal.models import MessageType, OrderAction, ParsedSignal
from app.signal.parser import SignalParser
from app.trading.risk import (
    RiskConfig,
    RiskContext,
    RiskDecision,
    RiskEngine,
    RiskStatus,
)
from app.core.timezone import format_vietnam_time, now_vietnam, set_broker_utc_offset_hours

logger = structlog.get_logger(__name__)


from app.trading.safety import TradingSafetyMixin
from app.telegram.presentation import execution_card
from app.trading.position_policy import PositionPolicyMixin
from app.trading.close_reporting import CloseReportingMixin
from app.trading.native_tp import NativeTPMixin, branch_comment, native_comment
from app.trading.management_scope import ManagementScopeMixin
from app.trading.slippage_limits import SlippageLimitMixin


class TradingService(SlippageLimitMixin, ManagementScopeMixin, NativeTPMixin, CloseReportingMixin, PositionPolicyMixin, TradingSafetyMixin):
    """Core trade coordinator and order router."""

    def __init__(
        self,
        broker: BrokerAdapter | None = None,
        parser: SignalParser | None = None,
        risk_engine: RiskEngine | None = None,
        risk_config: RiskConfig | None = None,
    ) -> None:
        self.broker = broker or MT5Executor()
        self.parser = parser or SignalParser()
        self.risk_engine = risk_engine or RiskEngine()
        if risk_config is not None:
            self.risk_config = risk_config
        else:
            try:
                from app.config.settings import settings
                self.risk_config = RiskConfig(
                    enabled=settings.trading.enabled,
                    max_open_positions=settings.trading.max_open_positions,
                    max_lot=Decimal(str(settings.trading.max_lot)),
                    min_lot=Decimal(str(settings.trading.min_lot)),
                    default_lot=Decimal(str(settings.trading.default_lot)),
                    max_daily_loss_percent=Decimal(str(settings.trading.max_daily_loss_percent)),
                    require_sl=settings.trading.require_sl,
                    require_tp=settings.trading.require_tp,
                    allowed_symbols=settings.trading.allowed_symbols
                    if isinstance(settings.trading.allowed_symbols, list)
                    else [s.strip() for s in settings.trading.allowed_symbols.split(",")],
                    signal_expire_seconds=settings.trading.signal_expire_seconds,
                    duplicate_timeout_seconds=settings.trading.duplicate_timeout_seconds,
                    enable_dual_entry=getattr(settings.trading, "enable_dual_entry", True),
                    lot_min_entry=Decimal(str(getattr(settings.trading, "lot_min_entry", 0.01))),
                    lot_max_entry=Decimal(str(getattr(settings.trading, "lot_max_entry", 0.01))),
                    max_slippage_entry=Decimal(str(getattr(settings.trading, "max_slippage_entry", 1.0))),
                )
            except Exception as exc:
                logger.error("invalid_startup_risk_config", error=str(exc))
                self.risk_config = RiskConfig(enabled=False)

        self._execution_lock = asyncio.Lock()
        self._close_executions: dict = {}
        self._recent_signals: list[dict[str, Any]] = []
        self._notify_callback: Callable[..., Coroutine[Any, Any, None]] | None = None
        self._pending_overrides: dict[int, dict[str, Any]] = {}
        self._tp1_processed_tickets: set[int] = set()
        self._tp1_completed_tickets: set[int] = set()
        self._signal_message_links: dict[int, str] = {}
        self._order_message_links: dict[int, str] = {}
        self._signal_msg_id_to_tickets: dict[int | tuple[int, int], list[int]] = {}
        self._signal_db_id_to_tickets: dict[int, list[int]] = {}
        self._ticket_to_signal_db_id: dict[int, int] = {}
        self._ticket_to_msg_id: dict[int, int] = {}
        self._ticket_to_symbol: dict[int, str] = {}
        self._ticket_to_entry_comment: dict[int, str] = {}
        self._cancelled_dual_entry_tickets: set[int] = set()
        self._position_opening_orders: dict[int, int] = {}
        self._position_entry_deals: dict[int, dict[int, DealInfo]] = {}
        self._pending_cancel_lock = asyncio.Lock()

    def remember_position_orders(self, deals) -> None:
        """Use only broker entry deals to associate stable position IDs with opening orders."""
        for deal in sorted(deals, key=lambda d: (d.time, d.ticket)):
            if deal.entry == "IN" and deal.position_id > 0 and deal.order > 0:
                self._position_opening_orders.setdefault(deal.position_id, deal.order)
                self._position_entry_deals.setdefault(deal.position_id, {})[deal.ticket] = deal

    async def with_position_entries(self, deals):
        """Recover entries older than the requested window by exact broker position ID."""
        entries = {d.position_id for d in deals if d.entry == "IN" and d.position_id > 0}
        missing = {d.position_id for d in deals
                   if d.position_id > 0 and d.entry in {"OUT", "OUT_BY", "INOUT"}} - entries
        result = {d.ticket: d for d in deals}
        self.remember_position_orders(deals)
        for position_id in sorted(missing):
            cached = self._position_entry_deals.get(position_id)
            if cached:
                result.update(cached)
                continue
            try:
                history = await self.broker.get_history_deals(position_id=position_id)
                for deal in history:
                    if deal.position_id == position_id and deal.entry == "IN":
                        result.setdefault(deal.ticket, deal)
            except Exception as exc:
                logger.warning("position_entry_history_unavailable", position_id=position_id, error=str(exc))
        values = list(result.values())
        self.remember_position_orders(values)
        return values

    def format_notification(self, text: str, message_link: str | None = None) -> str:
        """Attach clickable source message link to notification if available."""
        from app.telegram.utils import format_notification_with_link
        return format_notification_with_link(text, message_link)

    def set_notification_callback(
        self, callback: Callable[..., Coroutine[Any, Any, None]]
    ) -> None:
        """Register callback to push notification messages to Personal Telegram Bot."""
        self._notify_callback = callback

    async def notify(self, message: str, reply_markup=None) -> None:
        """Push a message to admin Telegram."""
        if self._notify_callback:
            try:
                import inspect
                kwargs = {}
                if reply_markup is not None:
                    kwargs["reply_markup"] = reply_markup
                try:
                    res = self._notify_callback(message, **kwargs)
                except TypeError:
                    res = self._notify_callback(message)
                if inspect.isawaitable(res):
                    await res
            except Exception as e:
                logger.error("telegram_notify_failed", error=str(e))

    def start_sync_job(self) -> None:
        """Start the background task to sync MT5 state with the DB."""
        import asyncio
        if getattr(self, "_sync_task", None) is None:
            self._sync_task = asyncio.create_task(self._sync_loop())

    async def stop_sync_job(self) -> None:
        """Stop the background MT5 sync task."""
        task = getattr(self, "_sync_task", None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._sync_task = None

    async def _sync_loop(self) -> None:
        import asyncio
        from datetime import datetime, timedelta
        
        # Start looking from 1 hour ago initially to catch up on missed DB updates
        self._last_sync_time = datetime.now() - timedelta(hours=1)
        _is_first_sync = True
        _last_reconnect_time = datetime.min
        
        while True:
            try:
                # Check broker connection before attempting any MT5 operations
                is_conn = await self.broker.is_connected()
                if not is_conn:
                    now_time = datetime.now()
                    if (now_time - _last_reconnect_time).total_seconds() > 15:
                        logger.warning("sync_loop_mt5_disconnected", msg="MT5 is not connected. Attempting auto-reconnect...")
                        await self.broker.connect()
                        _last_reconnect_time = now_time
                    await asyncio.sleep(5)
                    continue

                # Fetch new deals
                deals = await self.broker.get_history_deals(start_time=self._last_sync_time)
                # Complete identities without replaying historical entry notifications.
                await self.with_position_entries(deals)
                
                # Keep the previous cursor until all persistence in this batch succeeds.
                next_sync_time = datetime.now() - timedelta(seconds=2)
                
                # Check for updates in DB
                for deal in deals:
                    # In MT5, when a limit order fills, a deal is created with entry="IN"
                    if deal.entry == "IN" and deal.order > 0:
                        updated = await self._update_db_order_status(deal.order, "FILLED")
                        if updated and not _is_first_sync:
                            order_link = self._order_message_links.get(deal.order)
                            deal_time_str = format_vietnam_time(deal.time, "%H:%M:%S %d/%m/%Y")
                            filled_msg = (
                                f"🔔 <b>Lệnh Chờ Đã Khớp</b>\n"
                                f"━━━━━━━━━━━━━━━━\n"
                                f"• <b>{deal.symbol}</b> {deal.deal_type} <b>{deal.volume} lot</b> @ <code>{deal.price}</code>\n"
                                f"• Mã lệnh: <code>#{deal.position_id or deal.order}</code>\n"
                                f"⏱ {deal_time_str}"
                            )
                            await self.notify(self.format_notification(filled_msg, order_link))
                        
                    elif deal.entry in ("OUT", "OUT_BY", "INOUT"):
                        # Position closed (SL, TP or manual)
                        inserted = await self._insert_trade_record(deal)
                        if inserted and not _is_first_sync:
                            currency = ""
                            try:
                                acc = await self.broker.get_account_info()
                                if acc and isinstance(acc.currency, str):
                                    currency = acc.currency
                            except Exception:
                                pass
                            closed_msg, reason_str = await self.format_exit_notification(deal, currency)
                            await self.notify(closed_msg)

                            # Dual Entry: cancel remaining pending limit order if this position closed
                            closed_pos_ticket = deal.position_id
                            if closed_pos_ticket > 0:
                                await self.cancel_dual_entry_pending_orders_on_close(
                                    closed_pos_ticket, reason=reason_str
                                )
                
                _is_first_sync = False

                # Evaluate TP1 and TP2 milestones for open positions
                async with self._execution_lock:
                    await self.cancel_invalidated_slippage_limits()
                await self._check_tp_milestones()

                # Reconcile dual-entry pending orders (cancel remaining limit if sibling closed)
                await self._reconcile_dual_entry_pending_orders()
                self._last_sync_time = next_sync_time
            except asyncio.CancelledError:
                break
            except Exception as e:
                import traceback
                logger.error("sync_loop_error", error=str(e), traceback=traceback.format_exc())
                
            await asyncio.sleep(3)

    async def _insert_trade_record(self, deal: DealInfo) -> bool:
        try:
            from app.config.database import get_session
            from sqlalchemy.dialects.postgresql import insert
            from sqlalchemy import update
            from app.models.trade import Trade
            
            async with get_session() as session:
                if deal.position_id > 0:
                    await session.execute(update(Trade).where(Trade.mt5_ticket == deal.ticket,
                        Trade.mt5_position_id.is_(None)).values(
                            mt5_position_id=deal.position_id, mt5_order_ticket=deal.order or None))
                inserted = await session.execute(insert(Trade).values(
                        mt5_ticket=deal.ticket,
                        mt5_position_id=deal.position_id or None,
                        mt5_order_ticket=deal.order or None,
                        mt5_deal_type=0 if deal.deal_type == "BUY" else 1,
                        symbol=deal.symbol,
                        volume=deal.volume,
                        price=deal.price,
                        profit=deal.profit,
                        swap=deal.swap,
                        commission=deal.commission,
                        executed_at=deal.time
                    ).on_conflict_do_nothing(index_elements=[Trade.mt5_ticket]).returning(Trade.id))
                return inserted.scalar_one_or_none() is not None
        except Exception as e:
            logger.error("insert_trade_failed", error=str(e))
            raise

    async def _insert_management_action(self, signal_db_id: int | None, mt5_ticket: int, action_type: str, parameters: dict, status: str, result: dict) -> None:
        try:
            from app.config.database import get_session
            from app.models.trade import ManagementAction, Position
            from sqlalchemy import select
            
            async with get_session() as session:
                # Find position id by ticket
                stmt = select(Position.id).where(Position.mt5_ticket == mt5_ticket)
                pos_id = await session.scalar(stmt)
                
                action = ManagementAction(
                    signal_id=signal_db_id,
                    position_id=pos_id,
                    action_type=action_type,
                    parameters={**parameters, "ticket": mt5_ticket},
                    status=status,
                    result=result
                )
                session.add(action)
        except Exception as e:
            logger.error("insert_management_action_failed", error=str(e))
            self.risk_config.enabled = False
            raise

    async def _update_db_order_status(self, mt5_ticket: int, status: str) -> bool:
        """Returns True if a row was actually updated (meaning it was PLACED before)."""
        try:
            from app.config.database import get_session
            from sqlalchemy import update
            from app.models.trade import Order as DBOrder
            
            async with get_session() as session:
                stmt = (
                    update(DBOrder)
                    .where(DBOrder.mt5_ticket == mt5_ticket)
                    .where(DBOrder.status.in_(["PLACED", "PENDING"]))
                    .values(status=status)
                )
                res = await session.execute(stmt)
                return res.rowcount > 0
        except Exception as e:
            logger.error("db_order_update_failed", error=str(e))
            return False
    async def execute_override_order(self, signal_db_id: int, target: str) -> str:
        async with self._execution_lock:
            entry = self._pending_overrides.get(signal_db_id)
            if not entry or target not in {"both", "min", "max"}:
                return "❌ Lệnh chờ không còn hiệu lực."
            signal = entry["signal"]
            problem = self.signal_problem(signal, entry["signal_time"],
                                          skip_signal_expiry_check=entry.get('skip_signal_expiry_check', False))
            if problem:
                self._pending_overrides.pop(signal_db_id, None)
                await self.set_signal_status(entry.get("db_id"), "EXPIRED", problem)
                return f"⛔ {escape(problem)}"
            context = await self.build_risk_context()
            reqs = entry["orders"]
            targets = list(reqs) if target == "both" else [target]
            selected = [reqs[t] for t in targets if t in reqs]
            context.planned_order_count = sum(self.native_leg_count(req.tp1, req.tp2) for req in selected)
            # Only the explicitly presented rejection may be bypassed. All other rules remain active.
            allowed = entry.get("allowed_rule")
            for rule in self.risk_engine.rules:
                result = rule.evaluate(signal, context)
                if result.status == RiskStatus.REJECTED and result.rule_name != allowed:
                    return f"⛔ Không thể duyệt: {escape(result.reason)}"
            if not selected:
                return "❌ Lệnh đã được xử lý."
            prepared = []
            for req in selected:
                try:
                    tick = await self.checked_tick(req.symbol)
                    price = tick.ask if req.order_type.startswith("BUY") else tick.bid
                    # Keep pending targets; refresh the execution price only for market orders.
                    if req.order_type in {"BUY", "SELL"}:
                        req = replace(req, price=price)
                    problem = self.check_request(req)
                    if problem:
                        return f"⛔ {escape(problem)}"
                    await self.prepare_native_orders(req)
                    prepared.append(req)
                except Exception as exc:
                    return f"⛔ Không thể kiểm tra giá: {escape(str(exc))}"
            # Consume the approval before awaiting the broker, so repeated clicks cannot resubmit it.
            self._pending_overrides.pop(signal_db_id, None)
            messages = []
            successes = 0
            partial_branch = False
            if entry.get('slippage_limit'):
                await self.mark_slippage_limit(entry.get('db_id'))
            for req in prepared:
                if not self.risk_config.enabled:
                    messages.append("⛔ Bot đã tạm dừng; không gửi thêm lệnh.")
                    break
                result = await self.open_entry_branch(req)
                partial_branch |= bool(result.raw.get('partial_branch'))
                if result.raw.get("uncertain"):
                    self.risk_config.enabled = False
                    messages.append("⛔ Kết quả MT5 chưa xác định; đã dừng các nhánh tiếp theo để đối soát.")
                    break
                if result.success:
                    successes += 1
                    await self._record_db_order(entry.get("db_id"), result, req,
                        status="FILLED" if req.order_type in {"BUY", "SELL"} else "PLACED",
                        message_link=entry.get("message_link"), telegram_message_id=entry.get("message_id"))
                    messages.append(f"✅ {req.order_type} {escape(req.symbol)} · {req.volume} lot · #{result.ticket}")
                else:
                    messages.append(f"❌ {escape(result.comment)} (mã {result.retcode})")
            await self.set_signal_status(entry.get("db_id"),
                "EXECUTED" if successes == len(prepared) else ("PARTIAL" if successes or partial_branch else "ERROR"))
            if successes:
                self._recent_signals.append({"symbol": signal.symbol, "action": signal.new_order.action.value,
                                             "timestamp": time.time()})
            return "\n".join(messages)

    async def _record_db_order(
        self,
        signal_db_id: int | None,
        result: OrderResult,
        order_req: OrderRequest,
        status: str = "FILLED",
        message_link: str | None = None,
        telegram_message_id: int | None = None,
    ) -> None:
        """Persist executed or placed order into PostgreSQL audit trail."""
        if not result.success or not result.ticket:
            return
        if 'native_children' in result.raw:
            for child_req, child_result in result.raw['native_children']:
                await self._record_db_order(signal_db_id, child_result, child_req, status,
                                           message_link, telegram_message_id)
            return

        try:
            from app.config.database import get_session
            from app.models.trade import Order as DBOrder

            async with get_session() as session:
                from sqlalchemy.dialects.postgresql import insert
                await session.execute(insert(DBOrder).values(
                    signal_id=signal_db_id,
                    mt5_ticket=result.ticket,
                    symbol=order_req.symbol,
                    volume=result.volume or order_req.volume,
                    price=result.price or order_req.price,
                    sl=order_req.sl,
                    tp=order_req.tp,
                    tp1=order_req.tp1,
                    tp2=order_req.tp2,
                    comment=order_req.comment,
                    status=status,
                    mt5_retcode=result.retcode,
                    mt5_comment=result.comment,
                ).on_conflict_do_nothing(index_elements=[DBOrder.mt5_ticket]))
        except Exception as e:
            self.risk_config.enabled = False
            logger.error("db_order_record_failed", error=str(e))
            raise

        ticket = result.ticket
        self._ticket_to_entry_comment[ticket] = order_req.comment
        if message_link:
            self._order_message_links[ticket] = message_link

        if telegram_message_id is not None:
            tickets = self._signal_msg_id_to_tickets.setdefault(telegram_message_id, [])
            if ticket not in tickets:
                tickets.append(ticket)
            self._ticket_to_msg_id[ticket] = telegram_message_id

        if signal_db_id is not None:
            tickets = self._signal_db_id_to_tickets.setdefault(signal_db_id, [])
            if ticket not in tickets:
                tickets.append(ticket)
            self._ticket_to_signal_db_id[ticket] = signal_db_id

        self._ticket_to_symbol[ticket] = order_req.symbol

    async def cancel_dual_entry_pending_orders_on_close(
        self, closed_ticket: int, reason: str = ""
    ) -> list[int]:
        async with self._pending_cancel_lock:
            return await self._cancel_dual_entry_pending_orders_on_close(closed_ticket, reason)

    async def signal_sibling_tickets(self, ticket: int) -> set[int]:
        """Scope automatic cancellation to an exact stored signal, never just a message number."""
        order_ticket = self._position_opening_orders.get(ticket, ticket)
        signal_id = self._ticket_to_signal_db_id.get(order_ticket)
        groups = ([self._signal_db_id_to_tickets.get(signal_id, [])] if signal_id is not None
                  else [tickets for tickets in self._signal_db_id_to_tickets.values() if order_ticket in tickets])
        if len(groups) == 1 and order_ticket in groups[0]:
            return set(groups[0]) - {order_ticket}
        from sqlalchemy import or_, select
        from app.config.database import get_session
        from app.models.trade import Order as DBOrder, Position as DBPosition
        try:
            async with get_session() as session:
                signal_id = await session.scalar(select(DBOrder.signal_id)
                    .outerjoin(DBPosition, DBPosition.order_id == DBOrder.id)
                    .where(or_(DBOrder.mt5_ticket == order_ticket, DBPosition.mt5_ticket == ticket)))
                if signal_id is not None:
                    siblings = await session.scalars(select(DBOrder.mt5_ticket).where(
                        DBOrder.signal_id == signal_id, DBOrder.mt5_ticket.isnot(None),
                        DBOrder.mt5_ticket != order_ticket))
                    return set(siblings.all())
        except Exception as exc:
            logger.warning("signal_siblings_unavailable", ticket=ticket, error=str(exc))
        return set()

    async def _cancel_dual_entry_pending_orders_on_close(
        self, closed_ticket: int, reason: str = ""
    ) -> list[int]:
        """
        Cancel the same signal's waiting Max Entry after a confirmed Min Entry exit,
        including a partial close. Filled positions are never cancelled here.
        """
        if not self.risk_config.auto_cancel_pending_on_full_close:
            return []
        # A confirmed partial exit is sufficient; the remaining Min Entry may stay open.
        closed_ticket = self._position_opening_orders.get(closed_ticket, closed_ticket)
        sibling_tickets = await self.signal_sibling_tickets(closed_ticket)

        # 3. Check active pending orders from broker
        try:
            all_pending = await self.broker.get_pending_orders()
        except Exception as e:
            logger.error("get_pending_orders_for_dual_entry_cancel_failed", error=str(e))
            return []

        if not all_pending:
            return []
        if any(native_comment(po.comment) for po in all_pending if po.ticket in sibling_tickets):
            comment = self._ticket_to_entry_comment.get(closed_ticket)
            if comment is None:
                from sqlalchemy import select
                from app.config.database import get_session
                from app.models.trade import Order
                async with get_session() as session:
                    comment = await session.scalar(select(Order.comment).where(Order.mt5_ticket == closed_ticket))
            if not isinstance(comment, str) or branch_comment(comment) not in {'bot-min-entry', 'bot-override-min'}:
                return []

        # Find matching pending orders to cancel
        pending_to_cancel = [
            po for po in all_pending
            if po.ticket in sibling_tickets and po.ticket not in self._cancelled_dual_entry_tickets
            and branch_comment(getattr(po, "comment", "")) in {"bot-max-entry", "bot-override-max"}
        ]

        if not pending_to_cancel:
            return []

        cancelled_tickets: list[int] = []
        for po in pending_to_cancel:
            try:
                res = await self.broker.cancel_order(po.ticket)
                if res.success:
                    self._cancelled_dual_entry_tickets.add(po.ticket)
                    cancelled_tickets.append(po.ticket)
                    await self._update_db_order_status(po.ticket, "CANCELLED")

                    msg_link = (
                        self._order_message_links.get(po.ticket)
                        or self._order_message_links.get(closed_ticket)
                    )
                    reason_desc = f" ({reason})" if reason else ""
                    now_str = now_vietnam().strftime("%H:%M:%S %d/%m/%Y")
                    cancel_msg = (
                        f"🗑 <b>Hủy Lệnh Chờ Dual Entry</b>\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"• <b>{po.symbol}</b> {po.order_type} <b>{po.volume} lot</b> @ <code>{po.price}</code>\n"
                        f"• Ticket đã hủy: <code>{po.ticket}</code>\n"
                        f"• Lý do: Min Entry cùng signal đã có lần chốt{reason_desc}\n"
                        f"⏱ {now_str}"
                    )
                    await self.notify(self.format_notification(cancel_msg, msg_link))
                    logger.info(
                        "dual_entry_pending_order_cancelled",
                        cancelled_ticket=po.ticket,
                        closed_ticket=closed_ticket,
                        reason=reason,
                    )
                else:
                    logger.warning(
                        "dual_entry_cancel_order_failed",
                        ticket=po.ticket,
                        comment=res.comment,
                    )
            except Exception as e:
                logger.error("dual_entry_cancel_order_exception", ticket=po.ticket, error=str(e))

        return cancelled_tickets

    async def _reconcile_dual_entry_pending_orders(self) -> list[int]:
        """
        Recover missed cancellations after restart or a failed broker request,
        using confirmed exit deals even while the remaining Min Entry is open.
        """
        if not self.risk_config.auto_cancel_pending_on_full_close:
            return []
        try:
            pending_orders = await self.broker.get_pending_orders()
        except Exception as e:
            logger.error("reconcile_get_pending_orders_failed", error=str(e))
            return []

        if not pending_orders:
            return []

        try:
            history = await self.broker.get_history_deals(days=7)
            await self.with_position_entries(history)
            closed_tickets = {self._position_opening_orders.get(d.position_id, d.position_id)
                              for d in history if d.position_id > 0 and d.entry in {"OUT", "OUT_BY", "INOUT"}}
        except Exception:
            return []
        live_pending_tickets = {po.ticket for po in pending_orders}

        cancelled_tickets: list[int] = []

        for po in pending_orders:
            if branch_comment(getattr(po, "comment", "")) not in {"bot-max-entry", "bot-override-max"}:
                continue
            if po.ticket in self._cancelled_dual_entry_tickets:
                continue

            sibling_tickets = await self.signal_sibling_tickets(po.ticket)

            if not sibling_tickets:
                continue

            # A confirmed exit, including a partial one, triggers cancellation.
            should_cancel = False
            closed_sibling_ticket = None
            for s_ticket in sibling_tickets:
                is_pending = s_ticket in live_pending_tickets
                if not is_pending and s_ticket in closed_tickets:
                    should_cancel = True
                    closed_sibling_ticket = s_ticket
                    break

            if should_cancel and closed_sibling_ticket:
                cancelled_tickets.extend(await self.cancel_dual_entry_pending_orders_on_close(
                    closed_sibling_ticket, reason="Đã có deal chốt trong lịch sử"
                ))

        return cancelled_tickets

    async def get_tickets_for_reply_message(self, reply_to_msg_id: int, chat_id: int | None = None) -> list[int]:
        """Find MT5 tickets associated with the replied-to signal message ID."""
        tickets: list[int] = []
        cache_key = (chat_id, reply_to_msg_id) if chat_id is not None else reply_to_msg_id
        if cache_key in self._signal_msg_id_to_tickets:
            for t in self._signal_msg_id_to_tickets[cache_key]:
                if t not in tickets:
                    tickets.append(t)
            if tickets:
                return tickets

        try:
            from sqlalchemy import select
            from app.config.database import get_session
            from app.models.signal import Signal as DBSignal, SignalSource
            from app.models.trade import Order as DBOrder

            async with get_session() as session:
                stmt = (
                    select(DBOrder.mt5_ticket)
                    .join(DBSignal, DBOrder.signal_id == DBSignal.id)
                    .where(DBSignal.telegram_message_id == reply_to_msg_id)
                    .where(DBOrder.mt5_ticket.isnot(None))
                )
                if chat_id is not None:
                    stmt = stmt.join(SignalSource, DBSignal.source_id == SignalSource.id).where(SignalSource.chat_id == chat_id)
                db_tickets = list((await session.scalars(stmt)).all())
                for t in db_tickets:
                    if t not in tickets:
                        tickets.append(t)
                if tickets:
                    self._signal_msg_id_to_tickets[cache_key] = tickets
        except Exception as e:
            logger.warning("get_tickets_for_reply_db_error", reply_to_id=reply_to_msg_id, error=str(e))

        return tickets

    async def build_risk_context(self) -> RiskContext:
        """Construct live risk context from broker state."""
        try:
            account = await self.broker.get_account_info()
            positions = await self.broker.get_positions()
            connected = await self.broker.is_connected()
        except Exception as e:
            logger.warning("broker_context_fetch_failed", error=str(e))
            account = AccountInfo(
                login=0,
                balance=Decimal("0"),
                equity=Decimal("0"),
                margin=Decimal("0"),
                free_margin=Decimal("0"),
                leverage=1,
                server="",
                currency="USD",
            )
            positions = []
            connected = False

        daily_available = connected
        pnl, opening_balance = Decimal("0"), account.balance
        pending_count = 0
        try:
            pnl, opening_balance = await self.daily_risk_snapshot(account)
            pending_count = len(await self.broker.get_pending_orders())
        except Exception as exc:
            daily_available = False
            logger.warning("risk_snapshot_unavailable", error=str(exc))
        return RiskContext(
            today_realized_pnl=pnl,
            today_starting_balance=opening_balance,
            daily_history_available=daily_available,
            pending_order_count=pending_count,
            config=self.risk_config,
            account=account,
            open_positions=positions,
            recent_signals=self._recent_signals,
            broker_connected=connected,
        )

    async def process_raw_message(self, raw_text: str, **kwargs) -> None:
        # Signals and manual approvals share one execution lane.
        async with self._execution_lock:
            chat_id, message_id = kwargs.get("chat_id"), kwargs.get("message_id")
            if chat_id is not None and message_id is not None:
                from sqlalchemy.dialects.postgresql import insert
                from app.config.database import get_session
                from app.models.signal import SignalReceipt
                try:
                    # The transaction commits before any broker mutation.
                    async with get_session() as session:
                        result = await session.execute(insert(SignalReceipt).values(
                            chat_id=chat_id, message_id=message_id, raw_message=raw_text,
                            context={k: str(v) if isinstance(v, datetime) else v for k, v in kwargs.items()})
                            .on_conflict_do_nothing()
                            .returning(SignalReceipt.message_id))
                        claimed = result.scalar_one_or_none()
                        if claimed is None and kwargs.get('skip_signal_expiry_check'):
                            from app.trading.replay import reclaim_unexecuted_message
                            retry_context = {k: str(v) if isinstance(v, datetime) else v for k, v in kwargs.items()}
                            if await reclaim_unexecuted_message(session, chat_id, message_id, raw_text, retry_context):
                                claimed = message_id
                    if claimed is None:
                        await self.notify("⛔ Tin nhắn đã có yêu cầu giao dịch, đang chờ duyệt hoặc chưa đủ dữ liệu xác nhận an toàn; không thực thi lại.")
                        return
                except Exception as exc:
                    logger.error("signal_claim_failed", error=str(exc))
                    await self.notify("⛔ Không ghi được dấu xử lý tín hiệu vào database; chưa thực thi.")
                    return
            try:
                await self._process_raw_message(raw_text, **kwargs)
            except Exception as exc:
                logger.exception("signal_pipeline_failed", error=str(exc))
                await self.notify("❌ Xử lý tín hiệu gặp lỗi. Hãy đối chiếu MT5 và hộp thư; "
                                  "bot không tự gửi lại yêu cầu vì có thể lệnh đã được sàn nhận.")

    async def _process_raw_message(
        self,
        raw_text: str,
        is_reentry: bool = False,
        message_id: int | None = None,
        chat_id: int | None = None,
        message_link: str | None = None,
        reply_to_msg_id: int | None = None,
        signal_time: datetime | None = None,
        skip_signal_expiry_check: bool = False,
        reentry_origin: dict | None = None,
        management_scope: dict | None = None,
        related_message_path: list[int] | None = None,
    ) -> None:
        """Full pipeline: Raw message -> Parse -> Risk -> Execute -> Notify."""
        logger.info(
            "processing_signal_message",
            text=raw_text[:80],
            message_link=message_link,
            reply_to_msg_id=reply_to_msg_id,
        )

        if message_link is None and message_id is not None and chat_id is not None:
            from app.telegram.utils import construct_telegram_message_link
            message_link = construct_telegram_message_link(chat_id, message_id)

        signal_time = signal_time or datetime.now(timezone.utc)
        signal = self.parser.parse(raw_text)
        signal.is_reentry = is_reentry

        # 1. Audit trail: Persist raw signal into database
        signal_db_id: int | None = None
        try:
            from sqlalchemy import select
            from app.config.database import get_session
            from app.models.signal import ParsedSignal as DBParsedSignal
            from app.models.signal import Signal as DBSignal
            from app.models.signal import SignalSource

            async with get_session() as session:
                source_id = None
                if chat_id is not None:
                    stmt = select(SignalSource.id).where(SignalSource.chat_id == chat_id)
                    source_id = await session.scalar(stmt)
                    
                    if not source_id:
                        new_source = SignalSource(name=f"Chat {chat_id}", chat_id=chat_id)
                        session.add(new_source)
                        await session.flush()
                        source_id = new_source.id

                sig_record = DBSignal(
                    source_id=source_id,
                    telegram_message_id=message_id,
                    raw_message=raw_text,
                    message_type=signal.message_type.value,
                    status="RECEIVED",
                    parser_version=signal.parser_version,
                )
                session.add(sig_record)
                await session.flush()
                signal_db_id = sig_record.id

                if signal_db_id and message_link:
                    self._signal_message_links[signal_db_id] = message_link

                if signal.new_order:
                    meta = {"message_link": message_link} if message_link else {}
                    if reentry_origin is not None:
                        meta['reentry_origin'] = reentry_origin
                    parsed_record = DBParsedSignal(
                        signal_id=signal_db_id,
                        action=signal.new_order.action.value,
                        symbol=signal.new_order.symbol,
                        entry_min=signal.new_order.entry_min,
                        entry_max=signal.new_order.entry_max,
                        stop_loss=signal.new_order.stop_loss,
                        take_profit=signal.new_order.take_profit,
                        take_profit_2=signal.new_order.take_profit_2,
                        raw_tp_text=signal.new_order.raw_tp_text,
                        extra_metadata=meta or None,
                    )
                    session.add(parsed_record)
        except Exception as e:
            logger.warning("db_signal_record_failed", error=str(e))
            await self.notify("⛔ Không lưu được tín hiệu vào nhật ký; chưa thực thi.")
            return

        if signal.message_type == MessageType.UNKNOWN:
            await self.set_signal_status(signal_db_id, "UNKNOWN", signal.metadata.get("reason", "Không nhận diện được tín hiệu"))
            logger.info("signal_unknown_skipped", text=raw_text[:60])
            return

        # Preserve source levels in the audit above and adjust once for execution.
        # Override requests retain these adjusted levels without shifting them again.
        signal = self.adjusted_signal_levels(signal)
        problem = self.signal_problem(signal, signal_time, skip_signal_expiry_check=skip_signal_expiry_check)
        if problem:
            await self.set_signal_status(signal_db_id, "REJECTED", problem)
            await self.notify(self.format_notification(f"⛔ <b>Signal Rejected</b>\n{escape(problem)}", message_link))
            return
        cancelled_limits = await self.invalidate_slippage_limits(signal, chat_id, message_id, reply_to_msg_id,
            related_message_path or (reentry_origin or {}).get('message_path'), management_scope, is_reentry)
        context = await self.build_risk_context()

        # 2. New Order Flow
        if signal.message_type == MessageType.NEW_ORDER and signal.new_order:
            action = signal.new_order.action
            symbol = signal.new_order.symbol

            try:
                tick = await self.checked_tick(symbol)
                current_price = tick.bid if action == OrderAction.SELL else tick.ask
            except Exception as exc:
                await self.set_signal_status(signal_db_id, "REJECTED", str(exc))
                await self.notify(f"⛔ Không có giá thị trường hợp lệ: {escape(str(exc))}")
                return

            min_entry = signal.new_order.entry_min
            max_entry = signal.new_order.entry_max

            # Ensure min_entry is further from SL, max_entry is closer to SL
            if min_entry is not None and max_entry is not None and min_entry != max_entry:
                sl = signal.new_order.stop_loss
                if sl is not None:
                    if abs(min_entry - sl) < abs(max_entry - sl):
                        min_entry, max_entry = max_entry, min_entry
                elif action == OrderAction.BUY and min_entry < max_entry:
                    min_entry, max_entry = max_entry, min_entry
                elif action == OrderAction.SELL and min_entry > max_entry:
                    min_entry, max_entry = max_entry, min_entry

            has_dual_entry = (
                self.risk_config.enable_dual_entry
                and max_entry is not None
                and max_entry != min_entry
            )
            adverse_slippage = (current_price - min_entry if action == OrderAction.BUY else min_entry - current_price) if min_entry is not None else Decimal('0')
            slippage_limit = adverse_slippage > self.risk_config.max_slippage_entry

            # Determine pending order type
            def _get_pending_type(act: OrderAction, curr: Decimal, target: Decimal) -> str:
                if act == OrderAction.SELL:
                    return "SELL_LIMIT" if target > curr else "SELL_STOP"
                return "BUY_LIMIT" if target < curr else "BUY_STOP"

            sig_id = signal_db_id or int(time.time() * 1000)
            pending_reqs = {}
            vol_min = self.risk_config.lot_min_entry if has_dual_entry else self.risk_config.default_lot
            vol_max = self.risk_config.lot_max_entry

            if min_entry is not None:
                order_type_min = (
                    action.value
                    if abs(current_price - min_entry) <= self.risk_config.max_slippage_entry
                    else _get_pending_type(action, current_price, min_entry)
                )
                pending_reqs["min"] = OrderRequest(
                    symbol=symbol,
                    order_type=order_type_min,
                    volume=vol_min,
                    price=current_price if order_type_min == action.value else min_entry,
                    sl=signal.new_order.stop_loss,
                    tp=signal.new_order.take_profit_2 or signal.new_order.take_profit,
                    tp1=signal.new_order.take_profit,
                    tp2=signal.new_order.take_profit_2,
                    comment="bot-override-min",
                    signal_id=signal_db_id,
                )
            if has_dual_entry and max_entry is not None:
                pending_reqs["max"] = OrderRequest(
                    symbol=symbol,
                    order_type=_get_pending_type(action, current_price, max_entry),
                    volume=vol_max,
                    price=max_entry,
                    sl=signal.new_order.stop_loss,
                    tp=signal.new_order.take_profit_2 or signal.new_order.take_profit,
                    tp1=signal.new_order.take_profit,
                    tp2=signal.new_order.take_profit_2,
                    comment="bot-override-max",
                    signal_id=signal_db_id,
                )

            try:
                for request in pending_reqs.values():
                    await self.prepare_native_orders(request)
            except Exception as exc:
                await self.set_signal_status(signal_db_id, "REJECTED", str(exc))
                await self.notify(self.format_notification(f"⛔ {escape(str(exc))}", message_link))
                return
            context.planned_order_count = (2 if has_dual_entry else 1) * self.native_leg_count(signal.new_order.take_profit, signal.new_order.take_profit_2)
            override_data = {"signal": signal, "signal_time": signal_time, "db_id": signal_db_id,
                             "message_id": message_id, "allowed_rule": None,
                             "skip_signal_expiry_check": skip_signal_expiry_check}
            override_data['slippage_limit'] = slippage_limit
            decision = self.risk_engine.evaluate(signal, context)

            if not decision.approved:
                failed_results = [r for r in decision.results if r.status == RiskStatus.REJECTED]
                failed_rule = failed_results[0].rule_name if failed_results else ""
                failed_reason = failed_results[0].reason if failed_results else decision.rejection_reason

                # Safety limit rules: ask user via Telegram buttons instead of hard-rejecting
                OVERRIDABLE_RULES = {"duplicate_signal", "max_positions", "daily_loss_limit"}
                if failed_rule in OVERRIDABLE_RULES:
                    rule_titles = {
                        "duplicate_signal": "Trùng Tín Hiệu (Duplicate Signal)",
                        "max_positions": "Đạt Giới Hạn Vị Thế (Max Positions)",
                        "daily_loss_limit": "Chạm Giới Hạn Lỗ Ngày (Daily Loss Limit)",
                    }
                    title = rule_titles.get(failed_rule, "Giới Hạn Rủi Ro")
                    override_data["allowed_rule"] = failed_rule
                    entry_str = f"{min_entry} - {max_entry}" if has_dual_entry else f"{min_entry}"
                    warning_msg = (
                        f"⚠️ <b>Cảnh báo: {title}</b>\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"• Cặp: <b>{symbol}</b> ({action.value})\n"
                        f"• Vùng Entry: <code>{entry_str}</code> | Hiện tại: <code>{current_price}</code>\n"
                        f"• Lý do: <i>{failed_reason}</i>\n\n"
                        f"➡️ <b>Tạm dừng đặt lệnh tự động để bạn quyết định!</b>"
                    )
                    from app.telegram.keyboards import build_override_keyboard
                    self._pending_overrides[sig_id] = {**override_data, "orders": pending_reqs, "message_link": message_link}
                    await self.set_signal_status(signal_db_id, "AWAITING_APPROVAL", failed_reason)
                    kb = build_override_keyboard(sig_id, has_dual=has_dual_entry)
                    await self.notify(self.format_notification(warning_msg, message_link), reply_markup=kb)
                    return

                msg = (
                    f"⚠️ <b>Signal Rejected</b>\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"• Lệnh: <b>{signal.new_order.action.value} {signal.new_order.symbol}</b>\n"
                    f"• Lý do: {decision.rejection_reason}"
                )
                await self.set_signal_status(signal_db_id, "REJECTED", decision.rejection_reason)
                await self.notify(self.format_notification(msg, message_link))
                return

            if not has_dual_entry:
                vol_min = decision.calculated_lot
            
            # Check if price has completely exceeded the entire range (e.g. SELL and price > max_entry, or BUY and price < max_entry)
            if has_dual_entry and max_entry is not None:
                if action == OrderAction.SELL and current_price > max_entry:
                    warning_msg = (
                        f"⚠️ <b>Cảnh báo: Giá hiện tại vượt quá Max Entry</b>\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"• Cặp: <b>{symbol}</b> (SELL)\n"
                        f"• Vùng Entry: <code>{min_entry} - {max_entry}</code> | Hiện tại: <code>{current_price}</code>\n\n"
                        f"➡️ <i>Giá thị trường đã vượt quá vùng Entry, tạm dừng đặt lệnh!</i>"
                    )
                    from app.telegram.keyboards import build_override_keyboard
                    self._pending_overrides[sig_id] = {**override_data, "orders": pending_reqs, "message_link": message_link}
                    await self.set_signal_status(signal_db_id, "AWAITING_APPROVAL", "Giá vượt vùng vào lệnh")
                    kb = build_override_keyboard(sig_id, has_dual=has_dual_entry)
                    await self.notify(self.format_notification(warning_msg, message_link), reply_markup=kb)
                    return
                elif action == OrderAction.BUY and current_price < max_entry:
                    warning_msg = (
                        f"⚠️ <b>Cảnh báo: Giá hiện tại vượt quá Max Entry</b>\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"• Cặp: <b>{symbol}</b> (BUY)\n"
                        f"• Vùng Entry: <code>{min_entry} - {max_entry}</code> | Hiện tại: <code>{current_price}</code>\n\n"
                        f"➡️ <i>Giá thị trường đã vượt quá vùng Entry, tạm dừng đặt lệnh!</i>"
                    )
                    from app.telegram.keyboards import build_override_keyboard
                    self._pending_overrides[sig_id] = {**override_data, "orders": pending_reqs, "message_link": message_link}
                    await self.set_signal_status(signal_db_id, "AWAITING_APPROVAL", "Giá vượt vùng vào lệnh")
                    kb = build_override_keyboard(sig_id, has_dual=has_dual_entry)
                    await self.notify(self.format_notification(warning_msg, message_link), reply_markup=kb)
                    return

            # Adverse slippage waits for a pullback at the source entry, never chases price.
            if slippage_limit:
                await self.mark_slippage_limit(signal_db_id)

            order_req_1 = OrderRequest(
                symbol=symbol,
                order_type=action.value + "_LIMIT" if slippage_limit else action.value,
                volume=vol_min,
                price=min_entry if slippage_limit else current_price,
                sl=signal.new_order.stop_loss,
                tp=signal.new_order.take_profit_2 or signal.new_order.take_profit,
                tp1=signal.new_order.take_profit,
                tp2=signal.new_order.take_profit_2,
                comment="bot-min-entry" if has_dual_entry else "bot-market",
                    signal_id=signal_db_id,
            )
            problem = self.check_request(order_req_1)
            if problem:
                await self.set_signal_status(signal_db_id, "REJECTED", problem)
                await self.notify(f"⛔ {escape(problem)}")
                return
            if not self.risk_config.enabled:
                return
            result_1 = await self.open_entry_branch(order_req_1)
            await self.set_signal_status(signal_db_id, "EXECUTED" if result_1.success else
                                         "PARTIAL" if result_1.raw.get('partial_branch') else "ERROR", result_1.comment)
            if result_1.raw.get("uncertain"):
                self.risk_config.enabled = False
                await self.notify("⛔ Chưa xác định kết quả MT5; đã tạm dừng để đối soát, không gửi nhánh tiếp theo.")
                return

            if result_1.success:
                self._recent_signals.append(
                    {
                        "symbol": signal.symbol,
                        "action": action.value,
                        "timestamp": time.time(),
                    }
                )
                await self._record_db_order(
                    sig_id,
                    result_1,
                    order_req_1,
                    status="PLACED" if slippage_limit else "FILLED",
                    message_link=message_link,
                    telegram_message_id=message_id,
                )
                entry_label = "(Min Entry)" if has_dual_entry else ""
                entry_tag = f" {entry_label}" if entry_label else ""
                now_str = now_vietnam().strftime("%H:%M:%S %d/%m/%Y")
                execution_title = 'Limit Placed' if slippage_limit else 'Order Executed'
                msg_1 = execution_card(f"✅ {execution_title}{entry_tag}", order_req_1, result_1, now_str)
                if slippage_limit:
                    msg_1 += f'\nTrượt bất lợi {adverse_slippage} giá: chờ khớp lại tại entry của signal.'
            else:
                msg_1 = (
                    f"❌ <b>Order Execution Failed {'(Min Entry)' if has_dual_entry else ''}</b>\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"• Lệnh: {order_req_1.order_type} {order_req_1.symbol}\n"
                    f"• Lý do: {result_1.comment} (Mã lỗi: {result_1.retcode})"
                )
            await self.notify(self.format_notification(msg_1, message_link))

            # --- Order 2: Max Entry (Pending Limit - always placed when dual entry is enabled) ---
            if result_1.success and self.risk_config.enabled and has_dual_entry and max_entry is not None:
                vol_max = self.risk_config.lot_max_entry

                # Determine pending limit order type (only place if market has NOT touched max_entry yet)
                order2_type = None
                if action == OrderAction.SELL and max_entry > current_price:
                    order2_type = "SELL_LIMIT"
                    order2_price = max_entry
                elif action == OrderAction.BUY and max_entry < current_price:
                    order2_type = "BUY_LIMIT"
                    order2_price = max_entry

                if order2_type:
                    order_req_2 = OrderRequest(
                        symbol=symbol,
                        order_type=order2_type,
                        volume=vol_max,
                        price=order2_price,
                        sl=signal.new_order.stop_loss,
                        tp=signal.new_order.take_profit_2 or signal.new_order.take_profit,
                        tp1=signal.new_order.take_profit,
                        tp2=signal.new_order.take_profit_2,
                        comment="bot-max-entry",
                        signal_id=signal_db_id,
                    )
                    problem = self.check_request(order_req_2)
                    if problem:
                        await self.set_signal_status(signal_db_id, "PARTIAL", problem)
                        await self.notify(f"⛔ Nhánh thứ hai chưa được đặt: {escape(problem)}")
                        return
                    result_2 = await self.open_entry_branch(order_req_2)
                    if not result_2.success:
                        await self.set_signal_status(signal_db_id, "PARTIAL", result_2.comment)

                    if result_2.success:
                        await self._record_db_order(
                            sig_id,
                            result_2,
                            order_req_2,
                            status="PLACED",
                            message_link=message_link,
                            telegram_message_id=message_id,
                        )
                        now_str = now_vietnam().strftime("%H:%M:%S %d/%m/%Y")
                        msg_2 = execution_card("✅ Order Placed (Max Entry)", order_req_2, result_2, now_str)
                    else:
                        msg_2 = (
                            f"❌ <b>Order Failed (Max Entry)</b>\n"
                            f"━━━━━━━━━━━━━━━━\n"
                            f"• Lệnh: {order2_type} {symbol}\n"
                            f"• Lý do: {result_2.comment} (Mã lỗi: {result_2.retcode})"
                        )
                    await self.notify(self.format_notification(msg_2, message_link))

            return

        if signal.message_type == MessageType.POSITION_MANAGEMENT:
            reply_to_msg_id = await self.management_reply_id(chat_id, reply_to_msg_id, related_message_path)
            await self.process_management_signal(signal, signal_db_id, reply_to_msg_id, message_link,
                chat_id=chat_id, message_id=message_id, management_scope=management_scope,
                pre_cancelled_limits=cancelled_limits)

    # -------------------------------------------------------------
    # Admin Control Operations (called by Personal Telegram Bot)
    # -------------------------------------------------------------

    async def get_status_summary(self) -> dict[str, Any]:
        """Fetch system and account status."""
        context = await self.build_risk_context()
        acc = context.account
        return {
            "bot_enabled": self.risk_config.enabled, "connected": context.broker_connected,
            "login": acc.login, "server": acc.server, "balance": acc.balance, "equity": acc.equity,
            "margin": acc.margin, "free_margin": acc.free_margin,
            "open_positions_count": len(context.open_positions), "pending_orders_count": context.pending_order_count,
            "currency": acc.currency, "today_pnl": context.today_realized_pnl,
            "daily_available": context.daily_history_available,
            "awaiting_approval": len(self._pending_overrides),
        }

    async def close_position_by_ticket(
        self, ticket: int, volume: Decimal | None = None
    ) -> OrderResult:
        """Close an individual position."""
        grouped = await self.get_open_positions()
        target = next((p for p in grouped if p.ticket == ticket or any(c.ticket == ticket for c in p.members)), None)
        if target and target.members:
            percentage = Decimal('100') if volume is None else volume / target.volume * 100
            res, closed = await self.close_native_fraction(target, percentage)
            await self.record_management(None, target, 'CLOSE', res, reason='MANUAL_CLOSE', volume=str(closed))
            return res
        res = await self.broker.close_position(ticket, volume)
        metadata = self.close_execution_metadata(ticket, res, "MANUAL_CLOSE", partial=volume is not None or res.retcode == 10010)
        await self._insert_management_action(None, ticket, "CLOSE", {"reason": "MANUAL_CLOSE"},
            "EXECUTED" if res.success else "FAILED", {**metadata, "comment": res.comment, "retcode": res.retcode})
        if res.success:
            await self.cancel_dual_entry_pending_orders_on_close(ticket, reason="Đóng thủ công / Admin")
        return res

    async def close_all_positions(self) -> list[OrderResult]:
        """Emergency or manual close all open positions."""
        positions = await self.broker.get_positions()
        results: list[OrderResult] = []
        for pos in positions:
            res = await self.broker.close_position(pos.ticket, pos.volume)
            await self.record_management(None, pos, "CLOSE", res, reason="CLOSE_ALL")
            results.append(res)
            if res.success:
                await self.cancel_dual_entry_pending_orders_on_close(pos.ticket, reason="Đóng tất cả")
        return results

    async def modify_sl_tp(
        self,
        ticket: int,
        sl: Decimal | None = None,
        tp: Decimal | None = None,
    ) -> OrderResult:
        """Modify SL/TP on an active position."""
        positions = await self.get_open_positions()
        target = next((p for p in positions if p.ticket == ticket or any(c.ticket == ticket for c in p.members)), None)
        if target and target.members:
            results = [await self.broker.modify_position(p.ticket, sl, tp) for p in target.members]
            return OrderResult(all(r.success for r in results), ticket=ticket,
                               comment=' · '.join(r.comment for r in results))
        return await self.broker.modify_position(ticket, sl, tp)

    async def get_closed_deals(
        self, days: int = 7, limit: int = 15
    ) -> list[DealInfo]:
        """Retrieve recent closed trade deals (raw exit executions) from broker."""
        try:
            deals = await self.broker.get_history_deals(days=days)
            # Filter for closed deals: entry in ("OUT", "INOUT", "OUT_BY") or profit != 0,
            # and ignore balance operations (deals with no symbol or deal_type not BUY/SELL)
            closed = [
                d
                for d in deals
                if d.symbol
                and d.deal_type in ("BUY", "SELL")
                and (d.entry in ("OUT", "INOUT", "OUT_BY") or d.profit != Decimal("0"))
            ]
            closed.sort(key=lambda d: d.time, reverse=True)
            return closed[:limit]
        except Exception as e:
            logger.warning("get_closed_deals_failed", error=str(e))
            return []

    async def history_signal_link(self, position: ClosedPositionInfo) -> str | None:
        """Resolve the opening signal by exact position/order associations, including after restart."""
        tickets = {position.ticket, *position.opening_order_tickets}
        for ticket in sorted(tickets):
            link = self._order_message_links.get(ticket)
            signal_id = self._ticket_to_signal_db_id.get(ticket)
            link = link or self._signal_message_links.get(signal_id)
            if link:
                return link

        from sqlalchemy import or_, select
        from app.config.database import get_session
        from app.models.trade import Order as DBOrder, Position as DBPosition
        from app.models.signal import Signal as DBSignal, ParsedSignal as DBParsedSignal, SignalSource
        from app.telegram.utils import construct_telegram_message_link, parse_telegram_message_link

        try:
            async with get_session() as session:
                rows = await session.execute(
                    select(DBParsedSignal.extra_metadata, SignalSource.chat_id, DBSignal.telegram_message_id)
                    .select_from(DBOrder)
                    .join(DBSignal, DBSignal.id == DBOrder.signal_id)
                    .outerjoin(DBPosition, DBPosition.order_id == DBOrder.id)
                    .outerjoin(DBParsedSignal, DBParsedSignal.signal_id == DBSignal.id)
                    .outerjoin(SignalSource, SignalSource.id == DBSignal.source_id)
                    .where(or_(DBOrder.mt5_ticket.in_(tickets), DBPosition.mt5_ticket == position.ticket))
                    .distinct()
                )
                links = set()
                for metadata, chat_id, message_id in rows.all():
                    link = (metadata or {}).get("message_link")
                    if not isinstance(link, str) or parse_telegram_message_link(link)[1] is None:
                        link = construct_telegram_message_link(chat_id, message_id)
                    if link:
                        links.add(link)
                # Ambiguous associations must not send the user to a different signal.
                return links.pop() if len(links) == 1 else None
        except Exception as exc:
            logger.warning("history_signal_link_unavailable", ticket=position.ticket, error=str(exc))
            return None

    async def get_closed_positions(
        self, days: int = 7, limit: int | None = 15, *,
        start_time: datetime | None = None, end_time: datetime | None = None,
    ) -> list[ClosedPositionInfo]:
        """
        Retrieve closed positions aggregated across all partial exit deals.

        A single position can be closed in multiple partial steps (e.g. TP1, TP2, manual close).
        This method groups deals by position_id and computes volume-weighted average exit prices,
        aggregate profit, swap, commission, net PnL, and original position type (BUY/SELL).
        """
        try:
            deals = (await self.broker.get_history_deals(start_time=start_time) if start_time is not None
                     else await self.broker.get_history_deals(days=days))
            deals = await self.with_position_entries(deals)
        except Exception as e:
            logger.warning("get_closed_positions_history_failed", error=str(e))
            return []

        try:
            active_positions = await self.broker.get_positions()
            active_tickets = {identity for p in active_positions for identity in (p.ticket, p.identifier) if identity > 0}
            active_pos_map = {identity: p for p in active_positions for identity in (p.ticket, p.identifier) if identity > 0}
            positions_available = True
        except Exception as e:
            logger.warning("get_closed_positions_active_failed", error=str(e))
            active_positions = []
            active_tickets = set()
            active_pos_map = {}
            positions_available = False

        currency = "USD"
        try:
            acc = await self.broker.get_account_info()
            if acc and acc.currency:
                currency = acc.currency
        except Exception:
            pass

        from collections import defaultdict

        grouped_deals: dict[int, list[DealInfo]] = defaultdict(list)
        for d in deals:
            # Skip balance/deposit records and invalid deals
            if not d.symbol or d.deal_type not in ("BUY", "SELL"):
                continue
            pos_id = d.position_id
            if pos_id <= 0:
                # Keep an unresolved deal separate; never interpret a price or order
                # number in a broker comment as proof of position identity.
                pos_id = d.order if d.order > 0 else d.ticket
            if pos_id <= 0:
                continue
            grouped_deals[pos_id].append(d)

        missing_entry_pos_ids: list[int] = []
        aggregated: list[dict[str, Any]] = []
        from app.core.timezone import to_vietnam_datetime

        def in_period(deal):
            # MT5 pads history queries by a day at each end. Filter each exit
            # before aggregation so volume, PnL and partial details share the range.
            time = to_vietnam_datetime(deal.time)
            return ((start_time is None or time >= to_vietnam_datetime(start_time))
                    and (end_time is None or time < to_vietnam_datetime(end_time)))

        for pos_id, p_deals in grouped_deals.items():
            out_deals = [
                d
                for d in p_deals
                if in_period(d) and (d.entry in ("OUT", "INOUT", "OUT_BY")
                or (d.profit != Decimal("0") and d.entry != "IN"))
            ]
            if not out_deals:
                continue

            in_deals = [d for d in p_deals if d.entry == "IN"
                        and (end_time is None or to_vietnam_datetime(d.time) < to_vietnam_datetime(end_time))]

            total_vol = sum((d.volume for d in out_deals), Decimal("0"))
            if total_vol <= Decimal("0"):
                continue

            # Weighted average close price: sum(p * v) / total_vol
            weighted_close_sum = sum((d.price * d.volume for d in out_deals), Decimal("0"))
            close_price = (weighted_close_sum / total_vol).quantize(Decimal("0.00001"))

            profit = sum((d.profit for d in out_deals), Decimal("0"))
            swap = sum((d.swap for d in p_deals if in_period(d)), Decimal("0"))
            commission = sum((d.commission for d in p_deals if in_period(d)), Decimal("0"))
            net_pnl = profit + swap + commission

            symbol = out_deals[0].symbol or (in_deals[0].symbol if in_deals else "")

            # Infer position type:
            # 1. From IN deal if present
            # 2. From active positions if still partially open
            # 3. From opposite of OUT deal (closing BUY executes SELL deal in MT5)
            if in_deals:
                pos_type = in_deals[0].deal_type
            elif pos_id in active_pos_map:
                pos_type = active_pos_map[pos_id].position_type
            else:
                pos_type = "BUY" if out_deals[0].deal_type == "SELL" else "SELL"

            open_price: Decimal | None = None
            open_time: datetime | None = None
            initial_vol = total_vol
            if in_deals:
                total_in_vol = sum((d.volume for d in in_deals), Decimal("0"))
                if total_in_vol > Decimal("0"):
                    weighted_in_sum = sum((d.price * d.volume for d in in_deals), Decimal("0"))
                    open_price = (weighted_in_sum / total_in_vol).quantize(Decimal("0.00001"))
                    initial_vol = total_in_vol
                else:
                    open_price = in_deals[0].price
                open_time = min(d.time for d in in_deals)
            elif pos_id in active_pos_map:
                open_price = active_pos_map[pos_id].open_price
                open_time = active_pos_map[pos_id].open_time
                initial_vol = total_vol + active_pos_map[pos_id].volume
            else:
                missing_entry_pos_ids.append(pos_id)

            close_time = max(d.time for d in out_deals)
            is_fully_closed = (pos_id not in active_tickets
                               if positions_available and all(d.position_id > 0 for d in p_deals) else None)
            deals_count = len(out_deals)

            # Sort exit deals chronologically
            sorted_out_deals = sorted(out_deals, key=lambda d: d.time)
            partial_deals = [
                PartialCloseDealInfo(
                    ticket=d.ticket,
                    volume=d.volume,
                    price=d.price,
                    profit=d.profit + d.swap + d.commission,
                    time=d.time,
                )
                for d in sorted_out_deals
            ]

            comments = [d.comment for d in out_deals if d.comment]
            comment = comments[-1] if comments else ""

            aggregated.append(
                {
                    "ticket": pos_id,
                    "symbol": symbol,
                    "position_type": pos_type,
                    "volume": total_vol,
                    "total_volume": max(initial_vol, total_vol),
                    "open_price": open_price,
                    "close_price": close_price,
                    "profit": profit,
                    "swap": swap,
                    "commission": commission,
                    "net_pnl": net_pnl,
                    "currency": currency,
                    "open_time": open_time,
                    "close_time": close_time,
                    "deals_count": deals_count,
                    "is_fully_closed": is_fully_closed,
                    "partial_deals": partial_deals,
                    "opening_order_tickets": tuple(sorted({d.order for d in in_deals if d.order > 0})),
                    "comment": comment,
                }
            )

        # DB lookup for missing open_price / open_time
        if missing_entry_pos_ids:
            try:
                import inspect
                from sqlalchemy import select
                from app.config.database import get_session
                from app.models.trade import Order as DBOrder, Position as DBPosition

                async with get_session() as session:
                    stmt_pos = select(DBPosition).where(
                        DBPosition.mt5_ticket.in_(missing_entry_pos_ids)
                    )
                    res_pos = await session.scalars(stmt_pos)
                    items_pos = res_pos.all()
                    if inspect.isawaitable(items_pos):
                        items_pos = await items_pos
                    db_positions = {
                        p.mt5_ticket: p for p in items_pos if hasattr(p, "mt5_ticket")
                    }

                    stmt_ord = select(DBOrder).where(
                        DBOrder.mt5_ticket.in_(missing_entry_pos_ids)
                    )
                    res_ord = await session.scalars(stmt_ord)
                    items_ord = res_ord.all()
                    if inspect.isawaitable(items_ord):
                        items_ord = await items_ord
                    db_orders = {
                        o.mt5_ticket: o for o in items_ord if hasattr(o, "mt5_ticket")
                    }

                    for item in aggregated:
                        pid = item["ticket"]
                        if item["open_price"] is None:
                            if pid in db_positions and db_positions[pid].open_price is not None:
                                item["open_price"] = db_positions[pid].open_price
                                item["open_time"] = db_positions[pid].opened_at
                                if db_positions[pid].volume:
                                    item["total_volume"] = max(Decimal(str(db_positions[pid].volume)), item["volume"])
                            elif pid in db_orders and db_orders[pid].price is not None:
                                item["open_price"] = db_orders[pid].price
                                item["open_time"] = db_orders[pid].created_at
                                if db_orders[pid].volume:
                                    item["total_volume"] = max(Decimal(str(db_orders[pid].volume)), item["volume"])
            except Exception as e:
                logger.debug("db_entry_price_lookup_failed", error=str(e))

        result = [
            ClosedPositionInfo(
                ticket=item["ticket"],
                symbol=item["symbol"],
                position_type=item["position_type"],
                volume=item["volume"],
                open_price=item["open_price"],
                close_price=item["close_price"],
                profit=item["profit"],
                swap=item["swap"],
                commission=item["commission"],
                net_pnl=item["net_pnl"],
                open_time=item["open_time"],
                close_time=item["close_time"],
                deals_count=item["deals_count"],
                is_fully_closed=item["is_fully_closed"],
                total_volume=item["total_volume"],
                currency=item["currency"],
                partial_deals=item["partial_deals"],
                opening_order_tickets=item["opening_order_tickets"],
                comment=item["comment"],
            )
            for item in aggregated
        ]

        try:
            result = await self.group_native_history(result, active_positions, positions_available)
        except Exception as exc:
            logger.error('native_history_grouping_failed', error=str(exc))
        result.sort(key=lambda p: p.close_time, reverse=True)
        if limit is not None and limit > 0:
            return result[:limit]
        return result

    async def get_monthly_volume(self) -> dict[str, Any]:
        from app.trading.volume import monthly_volume
        from app.core.timezone import to_vietnam_datetime
        now = now_vietnam()
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        try:
            deals = await self.broker.get_history_deals(start_time=start)
            history = None
            if any(d.entry == "INOUT" and start <= to_vietnam_datetime(d.time) <= now for d in deals):
                try:
                    history = await self.broker.get_history_deals(start_time=datetime(1970, 1, 1, tzinfo=timezone.utc))
                except Exception as exc:
                    logger.warning("monthly_volume_reversal_history_unavailable", error=str(exc))
            return monthly_volume(deals, now, history)
        except Exception as exc:
            logger.warning("monthly_volume_unavailable", error=str(exc))
            return {"available": False, "month": start.strftime("%m/%Y")}

    async def get_performance_summary(self, days: int = 7) -> dict[str, Any]:
        """Compute performance metrics (win/loss, profit factor, net pnl) aggregated by position over the given days."""
        try:
            positions = await self.get_closed_positions(days=days, limit=None)
        except Exception as e:
            logger.warning("get_performance_positions_failed", error=str(e))
            positions = []

        total_trades = len(positions)
        wins = [p for p in positions if p.profit > Decimal("0")]
        losses = [p for p in positions if p.profit < Decimal("0")]
        breakevens = [p for p in positions if p.profit == Decimal("0")]

        total_profit = sum((p.profit for p in wins), Decimal("0"))
        total_loss = sum((p.profit for p in losses), Decimal("0"))  # negative Decimal
        total_swap = sum((p.swap for p in positions), Decimal("0"))
        total_commission = sum((p.commission for p in positions), Decimal("0"))
        net_pnl = total_profit + total_loss + total_swap + total_commission

        win_rate = (len(wins) / total_trades * 100) if total_trades > 0 else 0.0

        abs_loss = abs(total_loss)
        if abs_loss > Decimal("0"):
            profit_factor = float(total_profit / abs_loss)
        elif total_profit > Decimal("0"):
            profit_factor = 999.0
        else:
            profit_factor = 0.0

        best_trade = max((p.profit for p in positions), default=Decimal("0"))
        worst_trade = min((p.profit for p in positions), default=Decimal("0"))

        return {
            "days": days,
            "total_trades": total_trades,
            "win_trades": len(wins),
            "loss_trades": len(losses),
            "breakeven_trades": len(breakevens),
            "win_rate": round(win_rate, 1),
            "total_profit": total_profit,
            "total_loss": total_loss,
            "total_swap": total_swap,
            "total_commission": total_commission,
            "net_pnl": net_pnl,
            "profit_factor": round(profit_factor, 2),
            "best_trade": best_trade,
            "worst_trade": worst_trade,
        }

    # -------------------------------------------------------------
    # Vietnamese Keyword & Management Rules Operations
    # -------------------------------------------------------------

    def get_management_rules(self):
        """Get all currently active management rules in memory."""
        return list(self.parser.rule_engine._rules)

    async def load_management_rules_from_db(self) -> int:
        """Load active management rules from PostgreSQL into the rule engine."""
        try:
            from sqlalchemy import select
            from app.config.database import get_session
            from app.models.config import ManagementRule
            from app.signal.models import ManagementType
            from app.signal.rules import ManagementRuleDefinition

            async with get_session() as session:
                stmt = select(ManagementRule).where(ManagementRule.enabled == True)
                result = await session.execute(stmt)
                db_rules = result.scalars().all()

                if db_rules:
                    rule_defs = []
                    for r in db_rules:
                        try:
                            action_type = ManagementType(r.action_type)
                            rule_defs.append(
                                ManagementRuleDefinition(
                                    keyword=r.keyword,
                                    action_type=action_type,
                                    default_params=r.parameters or {},
                                    priority=r.priority,
                                )
                            )
                        except Exception as e:
                            logger.warning("invalid_db_rule_skipped", keyword=r.keyword, error=str(e))
                    if rule_defs:
                        self.parser.rule_engine.update_rules(rule_defs)
                        logger.info("loaded_management_rules_from_db", count=len(rule_defs))
                        return len(rule_defs)
        except Exception as e:
            logger.warning("load_management_rules_failed", error=str(e))
        return 0

    async def load_risk_config_from_db(self) -> RiskConfig:
        """Load all dynamic configuration from PostgreSQL bot_config table."""
        try:
            from sqlalchemy import select
            from app.config.database import get_session
            from app.models.config import BotConfig

            async with get_session() as session:
                stmt = select(BotConfig)
                result = await session.execute(stmt)
                configs = result.scalars().all()

                if configs:
                    updates = {}
                    for cfg in configs:
                        val = cfg.value
                        if isinstance(val, dict):
                            updates.update({k: v for k, v in val.items() if k in RiskConfig.model_fields})
                        elif cfg.key in RiskConfig.model_fields:
                            updates[cfg.key] = val
                    updates["breakeven_mode"] = "ticket"  # Migrate legacy setup averages.
                    candidate = self.risk_config.with_updates(updates)
                    for key, value in candidate.model_dump().items():
                        setattr(self.risk_config, key, value)
                    set_broker_utc_offset_hours(self.risk_config.broker_utc_offset_hours)

                    logger.info("loaded_risk_config_from_db", enabled=self.risk_config.enabled)
                    return self.risk_config
        except Exception as e:
            logger.warning("load_risk_config_from_db_failed", error=str(e))
            self.risk_config.enabled = False
        return self.risk_config

    async def save_risk_config_to_db(self, updated_by: str = "SYSTEM", config: RiskConfig | None = None) -> bool:
        """Persist current risk_config state to PostgreSQL bot_config table."""
        config = config if config is not None else self.risk_config
        try:
            from sqlalchemy import select
            from app.config.database import get_session
            from app.models.config import BotConfig

            grouped_configs = {
                ("trading", "general"): {
                    "enabled": bool(config.enabled),
                    "allowed_symbols": list(config.allowed_symbols),
                    "trading_hours": "ALL",
                },
                ("risk", "limits"): {
                    "max_open_positions": int(config.max_open_positions),
                    "max_lot": str(config.max_lot),
                    "min_lot": str(config.min_lot),
                    "default_lot": str(config.default_lot),
                    "max_daily_loss_percent": str(config.max_daily_loss_percent),
                    "require_sl": bool(config.require_sl),
                    "require_tp": bool(config.require_tp),
                },
                ("risk", "dual_entry"): {
                    "enable_dual_entry": bool(config.enable_dual_entry),
                    "lot_min_entry": str(config.lot_min_entry),
                    "lot_max_entry": str(config.lot_max_entry),
                    "max_slippage_entry": str(config.max_slippage_entry),
                },
                ("risk", "position_management"): {
                    "default_partial_close_percent": str(config.default_partial_close_percent),
                    "bad_entry_partial_close_percent": str(config.bad_entry_partial_close_percent),
                    "auto_breakeven_on_partial": bool(config.auto_breakeven_on_partial),
                    "breakeven_mode": config.breakeven_mode,
                    "breakeven_only_improve": config.breakeven_only_improve,
                    "require_explicit_close": config.require_explicit_close,
                    "generic_partial_uses_default": config.generic_partial_uses_default,
                    "tp1_enabled": config.tp1_enabled,
                    "tp1_min_volume_action": config.tp1_min_volume_action,
                    "auto_cancel_pending_on_full_close": config.auto_cancel_pending_on_full_close,
                    "breakeven_offset_price": str(config.breakeven_offset_price),
                    "allow_multiple_partial_closes": bool(config.allow_multiple_partial_closes),
                },
                ("signal", "timing"): {
                    "duplicate_timeout_seconds": int(config.duplicate_timeout_seconds),
                    "signal_expire_seconds": int(config.signal_expire_seconds),
                    "broker_utc_offset_hours": int(config.broker_utc_offset_hours),
                },
            }

            async with get_session() as session:
                for (cat, key), val in grouped_configs.items():
                    stmt = select(BotConfig).where(
                        BotConfig.category == cat,
                        BotConfig.key == key,
                    )
                    existing = (await session.execute(stmt)).scalar_one_or_none()
                    if existing:
                        merged = dict(existing.value or {})
                        merged.update(val)
                        existing.value = merged
                        existing.updated_by = updated_by
                    else:
                        cfg = BotConfig(
                            category=cat,
                            key=key,
                            value=val,
                            updated_by=updated_by,
                        )
                        session.add(cfg)
                await session.flush()
            logger.info("saved_risk_config_to_db")
            return True
        except Exception as e:
            logger.error("save_risk_config_to_db_failed", error=str(e))
            return False

    async def update_config_key(self, key: str, value: Any, updated_by: str = "ADMIN") -> bool:
        return (await self.update_config_keys({key: value}, updated_by)).get(key, False)

    async def update_config_keys(self, updates: dict[str, Any], updated_by: str = "ADMIN",
                                 expected: dict | None = None) -> dict[str, bool]:
        """Validate a complete batch and roll back memory if persistence fails."""
        async with self._execution_lock:
            if expected is not None and self.risk_config.model_dump(mode="json") != expected:
                return dict.fromkeys(updates, False)
            try:
                candidate = self.risk_config.with_updates(updates)
            except (ValueError, TypeError):
                return dict.fromkeys(updates, False)
            if not await self.save_risk_config_to_db(updated_by=updated_by, config=candidate):
                return dict.fromkeys(updates, False)
            for key, value in candidate.model_dump().items():
                setattr(self.risk_config, key, value)
            set_broker_utc_offset_hours(self.risk_config.broker_utc_offset_hours)
            return dict.fromkeys(updates, True)

    async def add_or_update_management_rule(
        self, keyword: str, action_type_str: str, percentage: Decimal | None = None,
        target: str | None = None, priority: int = 5,
    ) -> bool:
        async with self._execution_lock:
            return await self._add_or_update_management_rule(keyword, action_type_str, percentage, target, priority)

    async def _add_or_update_management_rule(
        self,
        keyword: str,
        action_type_str: str,
        percentage: Decimal | None = None,
        target: str | None = None,
        priority: int = 5,
    ) -> bool:
        """Add or update a management rule in memory and persist to DB."""
        clean_kw = keyword.lower().strip()
        from app.signal.models import ManagementType
        try:
            action_type = ManagementType(action_type_str.upper().strip())
        except ValueError:
            return False

        params: dict[str, Any] = {}
        if percentage is not None:
            params["percentage"] = percentage
        if target is not None:
            params["target"] = target
        elif action_type in (ManagementType.MOVE_SL, ManagementType.BREAKEVEN):
            params["target"] = "ENTRY"

        try:
            from sqlalchemy import select
            from app.config.database import get_session
            from app.models.config import ManagementRule

            async with get_session() as session:
                stmt = select(ManagementRule).where(ManagementRule.keyword == clean_kw)
                existing = (await session.execute(stmt)).scalar_one_or_none()
                db_params = {k: float(v) if isinstance(v, Decimal) else v for k, v in params.items()}
                if existing:
                    existing.action_type = action_type.value
                    existing.parameters = db_params
                    existing.priority = priority
                    existing.enabled = True
                else:
                    new_rule = ManagementRule(
                        keyword=clean_kw,
                        action_type=action_type.value,
                        parameters=db_params,
                        priority=priority,
                        enabled=True,
                    )
                    session.add(new_rule)
            self.parser.rule_engine.add_or_update_rule(
                keyword=clean_kw, action_type=action_type, params=params, priority=priority)
            return True
        except Exception as e:
            logger.error("save_management_rule_db_failed", keyword=clean_kw, error=str(e))
            return False

    async def remove_management_rule(self, keyword: str) -> bool:
        async with self._execution_lock:
            return await self._remove_management_rule(keyword)

    async def _remove_management_rule(self, keyword: str) -> bool:
        """Remove a rule from memory and database."""
        clean_kw = keyword.lower().strip()

        try:
            from sqlalchemy import delete
            from app.config.database import get_session
            from app.models.config import ManagementRule

            async with get_session() as session:
                stmt = delete(ManagementRule).where(ManagementRule.keyword == clean_kw)
                await session.execute(stmt)
        except Exception as e:
            logger.error("delete_management_rule_db_failed", keyword=clean_kw, error=str(e))
            return False
        return self.parser.rule_engine.remove_rule(clean_kw)


# Global singleton instance
trading_service = TradingService()
