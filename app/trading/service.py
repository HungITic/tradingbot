"""
Trading Service — coordinates signals, risk evaluation, execution, and admin commands.
"""

from __future__ import annotations

import time
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


class TradingService:
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
            except Exception:
                self.risk_config = RiskConfig()

        self._recent_signals: list[dict[str, Any]] = []
        self._notify_callback: Callable[..., Coroutine[Any, Any, None]] | None = None
        self._pending_overrides: dict[int, dict[str, Any]] = {}
        self._tp1_processed_tickets: set[int] = set()
        self._signal_message_links: dict[int, str] = {}
        self._order_message_links: dict[int, str] = {}
        self._signal_msg_id_to_tickets: dict[int, list[int]] = {}
        self._signal_db_id_to_tickets: dict[int, list[int]] = {}
        self._ticket_to_signal_db_id: dict[int, int] = {}
        self._ticket_to_msg_id: dict[int, int] = {}
        self._ticket_to_symbol: dict[int, str] = {}
        self._cancelled_dual_entry_tickets: set[int] = set()

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
                
                # Update last sync time slightly overlapping
                self._last_sync_time = datetime.now() - timedelta(seconds=2)
                
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
                                f"• Ticket: <code>{deal.ticket}</code> (Lệnh chờ: <code>{deal.order}</code>)\n"
                                f"⏱ {deal_time_str}"
                            )
                            await self.notify(self.format_notification(filled_msg, order_link))
                        
                    elif deal.entry in ("OUT", "OUT_BY", "INOUT"):
                        # Position closed (SL, TP or manual)
                        inserted = await self._insert_trade_record(deal)
                        if inserted and not _is_first_sync:
                            if deal.reason == 3:
                                reason_str = "Dính SL"
                            elif deal.reason == 4:
                                reason_str = "Chốt lời TP"
                            else:
                                reason_str = "Đóng lệnh"
                                
                            icon = "🟢" if deal.profit > 0 else ("🔴" if deal.profit < 0 else "⚪")
                            deal_link = self._order_message_links.get(deal.order) or self._order_message_links.get(deal.ticket)
                            deal_time_str = format_vietnam_time(deal.time, "%d/%m/%Y %H:%M:%S")

                            # In MT5, exit deal direction is opposite of original position
                            pos_type_title = "Sell" if deal.deal_type == "BUY" else "Buy"
                            badge_icon = "🔴" if pos_type_title == "Sell" else "🟢"
                            closed_pos_ticket = deal.position_id if deal.position_id > 0 else deal.order

                            currency = "USD"
                            try:
                                acc = await self.broker.get_account_info()
                                if acc and acc.currency:
                                    currency = acc.currency
                            except Exception:
                                pass

                            if deal.profit % Decimal("1") == Decimal("0"):
                                pnl_str = f"{deal.profit:+,.0f} {currency}"
                            else:
                                pnl_str = f"{deal.profit:+,.2f} {currency}"

                            price_str = f"{deal.price:,.2f}" if deal.price >= Decimal("50") else f"{deal.price:,.4f}"

                            closed_msg = (
                                f"{badge_icon} <b>{pos_type_title} {deal.symbol}</b>  (<code>#{closed_pos_ticket}</code>)\n"
                                f"━━━━━━━━━━━━━━━━\n"
                                f"<b>Kết thúc PnL:</b> {icon} <b>{pnl_str}</b>\n"
                                f"<b>Khối lượng đóng:</b> <code>{deal.volume} Lots</code>\n"
                                f"<b>Giá đóng:</b> <code>{price_str}</code>\n"
                                f"<b>Thời gian đóng:</b> <code>{deal_time_str}</code>\n"
                                f"<i>Lý do: {reason_str}</i>"
                            )
                            await self.notify(self.format_notification(closed_msg, deal_link))

                            # Dual Entry: cancel remaining pending limit order if this position closed
                            closed_pos_ticket = deal.position_id if deal.position_id > 0 else deal.order
                            if closed_pos_ticket > 0:
                                await self.cancel_dual_entry_pending_orders_on_close(
                                    closed_pos_ticket, reason=reason_str
                                )
                
                _is_first_sync = False

                # Evaluate TP1 and TP2 milestones for open positions
                await self._check_tp_milestones()

                # Reconcile dual-entry pending orders (cancel remaining limit if sibling closed)
                await self._reconcile_dual_entry_pending_orders()
            except Exception as e:
                import traceback
                logger.error("sync_loop_error", error=str(e), traceback=traceback.format_exc())
                
            await asyncio.sleep(3)

    async def _check_tp_milestones(self) -> None:
        """
        Evaluate live open positions against TP1 and TP2 milestones:
        - TP1 reached: check if partially closed before. If already closed, skip.
                       If not, close 50% and move SL to Entry.
        - TP2 reached: full close 100%.
        """
        try:
            positions = await self.broker.get_positions()
            if not positions:
                return

            tickets = [p.ticket for p in positions]
            from app.config.database import get_session
            from app.models.trade import Order as DBOrder
            from sqlalchemy import select

            async with get_session() as session:
                stmt = select(DBOrder).where(DBOrder.mt5_ticket.in_(tickets))
                db_orders = {o.mt5_ticket: o for o in (await session.scalars(stmt)).all()}

            for pos in positions:
                db_order = db_orders.get(pos.ticket)
                if not db_order:
                    continue

                tp1 = db_order.tp1
                tp2 = db_order.tp2
                if tp1 is None and tp2 is None:
                    continue

                current_price = pos.current_price
                is_buy = pos.position_type == "BUY"

                pos_link = self._order_message_links.get(pos.ticket) or (self._signal_message_links.get(db_order.signal_id) if db_order.signal_id else None)

                # 1. Check TP2 first (Full Close)
                tp2_reached = False
                if tp2 is not None:
                    if is_buy and current_price >= tp2:
                        tp2_reached = True
                    elif not is_buy and current_price <= tp2:
                        tp2_reached = True

                if tp2_reached:
                    res = await self.broker.close_position(pos.ticket, pos.volume)
                    status_icon = "🎯" if res.success else "❌"
                    now_str = now_vietnam().strftime("%H:%M:%S %d/%m/%Y")
                    tp2_msg = (
                        f"{status_icon} <b>Đạt TP2 ({tp2}): Chốt Lời Toàn Bộ</b>\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"• <b>{pos.symbol}</b> ({pos.position_type}) <b>{pos.volume} lot</b> @ <code>{current_price}</code>\n"
                        f"• Ticket: <code>{pos.ticket}</code> | Kết quả: {res.comment}\n"
                        f"⏱ {now_str}"
                    )
                    await self.notify(self.format_notification(tp2_msg, pos_link))
                    if res.success:
                        await self.cancel_dual_entry_pending_orders_on_close(
                            pos.ticket, reason=f"Đạt TP2 ({tp2})"
                        )
                    await self._insert_management_action(
                        signal_db_id=db_order.signal_id,
                        mt5_ticket=pos.ticket,
                        action_type="CLOSE",
                        parameters={"reason": "TP2_REACHED", "price": float(current_price), "tp2": float(tp2)},
                        status="EXECUTED" if res.success else "FAILED",
                        result={"comment": res.comment, "retcode": res.retcode},
                    )
                    self._tp1_processed_tickets.discard(pos.ticket)
                    continue

                # 2. Check TP1 (Partial Close 50% & Move SL to Entry)
                tp1_reached = False
                if tp1 is not None:
                    if is_buy and current_price >= tp1:
                        tp1_reached = True
                    elif not is_buy and current_price <= tp1:
                        tp1_reached = True

                if tp1_reached:
                    already_closed = (
                        (db_order.volume is not None and pos.volume < db_order.volume)
                        or (pos.ticket in self._tp1_processed_tickets)
                    )
                    if already_closed:
                        continue

                    vol_to_close = (pos.volume / Decimal("2")).quantize(Decimal("0.01"))
                    vol_to_close = max(Decimal("0.01"), min(vol_to_close, pos.volume))

                    if pos.volume <= Decimal("0.01"):
                        be_res = await self.broker.modify_position(pos.ticket, sl=pos.open_price)
                        self._tp1_processed_tickets.add(pos.ticket)
                        be_icon = "🎯" if be_res.success else "⚠️"
                        now_str = now_vietnam().strftime("%H:%M:%S %d/%m/%Y")
                        tp1_be_msg = (
                            f"{be_icon} <b>Đạt TP1 ({tp1}): Dời SL Về Entry</b>\n"
                            f"━━━━━━━━━━━━━━━━\n"
                            f"• Ticket: <code>{pos.ticket}</code> ({pos.symbol})\n"
                            f"• SL mới: <code>{pos.open_price}</code> (Hòa vốn)\n"
                            f"<i>Volume {pos.volume} lot tối thiểu không thể chia nhỏ, dời SL về Entry để gồng TP2.</i>\n"
                            f"⏱ {now_str}"
                        )
                        await self.notify(self.format_notification(tp1_be_msg, pos_link))
                        await self._insert_management_action(
                            signal_db_id=db_order.signal_id,
                            mt5_ticket=pos.ticket,
                            action_type="MOVE_SL",
                            parameters={"reason": "TP1_REACHED_MIN_LOT", "sl": float(pos.open_price), "tp1": float(tp1)},
                            status="EXECUTED" if be_res.success else "FAILED",
                            result={"comment": be_res.comment, "retcode": be_res.retcode},
                        )
                    else:
                        close_res = await self.broker.close_position(pos.ticket, vol_to_close)
                        be_res = await self.broker.modify_position(pos.ticket, sl=pos.open_price)
                        self._tp1_processed_tickets.add(pos.ticket)
                        icon = "🎯" if close_res.success else "❌"
                        rem_vol = pos.volume - vol_to_close
                        now_str = now_vietnam().strftime("%H:%M:%S %d/%m/%Y")
                        tp1_part_msg = (
                            f"{icon} <b>Đạt TP1 ({tp1}): Chốt 50% & Dời SL</b>\n"
                            f"━━━━━━━━━━━━━━━━\n"
                            f"• Ticket: <code>{pos.ticket}</code> ({pos.symbol})\n"
                            f"• Đã chốt: <b>{vol_to_close} lot</b> | Còn lại: <b>{rem_vol} lot</b>\n"
                            f"• SL mới: <code>{pos.open_price}</code> (Entry hòa vốn)\n"
                            f"⏱ {now_str}"
                        )
                        await self.notify(self.format_notification(tp1_part_msg, pos_link))
                        await self._insert_management_action(
                            signal_db_id=db_order.signal_id,
                            mt5_ticket=pos.ticket,
                            action_type="PARTIAL_CLOSE",
                            parameters={"reason": "TP1_REACHED", "volume": float(vol_to_close), "sl": float(pos.open_price), "tp1": float(tp1)},
                            status="EXECUTED" if close_res.success else "FAILED",
                            result={"comment": close_res.comment, "retcode": close_res.retcode},
                        )
        except Exception as e:
            logger.error("check_tp_milestones_failed", error=str(e))

    async def _insert_trade_record(self, deal: DealInfo) -> bool:
        try:
            from app.config.database import get_session
            from sqlalchemy import select
            from app.models.trade import Trade
            
            async with get_session() as session:
                # Check if deal already inserted
                stmt = select(Trade.id).where(Trade.mt5_ticket == deal.ticket)
                exists = await session.scalar(stmt)
                if not exists:
                    trade = Trade(
                        mt5_ticket=deal.ticket,
                        mt5_deal_type=0 if deal.deal_type == "BUY" else 1,
                        symbol=deal.symbol,
                        volume=deal.volume,
                        price=deal.price,
                        profit=deal.profit,
                        swap=deal.swap,
                        commission=deal.commission,
                        executed_at=deal.time
                    )
                    session.add(trade)
                    # Commit handled by context manager
                    return True
                return False
        except Exception as e:
            logger.error("insert_trade_failed", error=str(e))
            return False

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
                    parameters=parameters,
                    status=status,
                    result=result
                )
                session.add(action)
        except Exception as e:
            logger.error("insert_management_action_failed", error=str(e))

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
                    .where(DBOrder.status == "PLACED") # Only update if it's currently PLACED
                    .values(status=status)
                )
                res = await session.execute(stmt)
                return res.rowcount > 0
        except Exception as e:
            logger.error("db_order_update_failed", error=str(e))
            return False
    async def execute_override_order(self, signal_db_id: int, target: str) -> str:
        """Executes a pending order override (min or max) and returns the result message."""
        entry = self._pending_overrides.get(signal_db_id)
        if not entry:
            return "❌ Lệnh chờ không còn hiệu lực hoặc đã quá hạn."
        
        orig_msg_id = None
        if isinstance(entry, dict) and "orders" in entry:
            reqs = entry["orders"]
            msg_link = entry.get("message_link")
            orig_msg_id = entry.get("message_id")
        else:
            reqs = entry
            msg_link = None
        
        targets_to_execute = list(reqs.keys()) if target == "both" else [target]
        results = []
        
        for t in targets_to_execute:
            req = reqs.pop(t, None)
            if not req:
                results.append(f"❌ Không tìm thấy lệnh chờ {t}.")
                continue
                
            result = await self.broker.open_order(req)
            if result.success:
                if msg_link and result.ticket:
                    self._order_message_links[result.ticket] = msg_link
                await self._record_db_order(
                    signal_db_id,
                    result,
                    req,
                    status="PLACED",
                    message_link=msg_link,
                    telegram_message_id=orig_msg_id,
                )
                results.append(
                    f"✅ <b>Order Placed ({t.capitalize()} Entry Override)</b>\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"• Ticket: <code>{result.ticket}</code>\n"
                    f"• Khối lượng: <b>{result.volume or req.volume} lot</b> @ <code>{req.price}</code>"
                )
            else:
                results.append(
                    f"❌ <b>Order Failed ({t.capitalize()} Entry Override)</b>\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"• Lý do: {result.comment} (Mã lỗi: {result.retcode})"
                )
        
        if not reqs:
            self._pending_overrides.pop(signal_db_id, None)
            
        return "\n\n".join(results)

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

        ticket = result.ticket
        if message_link:
            self._order_message_links[ticket] = message_link

        if telegram_message_id is not None:
            self._signal_msg_id_to_tickets.setdefault(telegram_message_id, []).append(ticket)
            self._ticket_to_msg_id[ticket] = telegram_message_id

        if signal_db_id is not None:
            self._signal_db_id_to_tickets.setdefault(signal_db_id, []).append(ticket)
            self._ticket_to_signal_db_id[ticket] = signal_db_id

        self._ticket_to_symbol[ticket] = order_req.symbol

        if not signal_db_id:
            return
        try:
            from app.config.database import get_session
            from app.models.trade import Order as DBOrder

            async with get_session() as session:
                order_rec = DBOrder(
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
                )
                session.add(order_rec)
        except Exception as e:
            logger.warning("db_order_record_failed", error=str(e))

    async def cancel_dual_entry_pending_orders_on_close(
        self, closed_ticket: int, reason: str = ""
    ) -> list[int]:
        """
        When an order/position of a dual-entry setup closes (due to TP, SL, or manual close),
        automatically cancel any remaining pending orders (e.g. Max Entry limit order)
        belonging to that same signal.
        """
        sibling_tickets: set[int] = set()

        # 1. Resolve from in-memory tracking
        sig_db_id = self._ticket_to_signal_db_id.get(closed_ticket)
        if sig_db_id and sig_db_id in self._signal_db_id_to_tickets:
            for t in self._signal_db_id_to_tickets[sig_db_id]:
                if t != closed_ticket:
                    sibling_tickets.add(t)

        msg_id = self._ticket_to_msg_id.get(closed_ticket)
        if msg_id and msg_id in self._signal_msg_id_to_tickets:
            for t in self._signal_msg_id_to_tickets[msg_id]:
                if t != closed_ticket:
                    sibling_tickets.add(t)

        if not sibling_tickets:
            for s_tickets in self._signal_db_id_to_tickets.values():
                if closed_ticket in s_tickets:
                    for t in s_tickets:
                        if t != closed_ticket:
                            sibling_tickets.add(t)
            for m_tickets in self._signal_msg_id_to_tickets.values():
                if closed_ticket in m_tickets:
                    for t in m_tickets:
                        if t != closed_ticket:
                            sibling_tickets.add(t)

        # 2. Query Database (PostgreSQL) if not found in memory
        if not sibling_tickets:
            try:
                from sqlalchemy import select
                from app.config.database import get_session
                from app.models.trade import Order as DBOrder

                async with get_session() as session:
                    stmt = select(DBOrder.signal_id).where(DBOrder.mt5_ticket == closed_ticket)
                    signal_id = await session.scalar(stmt)
                    if signal_id:
                        stmt2 = select(DBOrder.mt5_ticket).where(
                            DBOrder.signal_id == signal_id,
                            DBOrder.mt5_ticket.isnot(None),
                            DBOrder.mt5_ticket != closed_ticket,
                        )
                        db_siblings = (await session.scalars(stmt2)).all()
                        for t in db_siblings:
                            sibling_tickets.add(t)
            except Exception as e:
                logger.debug("resolve_db_siblings_error", error=str(e), closed_ticket=closed_ticket)

        # 3. Check active pending orders from broker
        try:
            all_pending = await self.broker.get_pending_orders()
        except Exception as e:
            logger.error("get_pending_orders_for_dual_entry_cancel_failed", error=str(e))
            return []

        if not all_pending:
            return []

        # Find matching pending orders to cancel
        pending_to_cancel = [
            po for po in all_pending
            if po.ticket in sibling_tickets and po.ticket not in self._cancelled_dual_entry_tickets
        ]

        if not pending_to_cancel:
            return []

        cancelled_tickets: list[int] = []
        for po in pending_to_cancel:
            self._cancelled_dual_entry_tickets.add(po.ticket)
            try:
                res = await self.broker.cancel_order(po.ticket)
                if res.success:
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
                        f"• Lý do: Lệnh song sinh (Ticket <code>{closed_ticket}</code>) đã đóng{reason_desc}\n"
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
        Reconcile active pending orders against active positions.
        If any pending limit order belongs to a dual-entry signal whose
        sibling order was placed/filled but is no longer open in active positions,
        cancel the remaining pending order immediately.
        """
        try:
            pending_orders = await self.broker.get_pending_orders()
        except Exception as e:
            logger.error("reconcile_get_pending_orders_failed", error=str(e))
            return []

        if not pending_orders:
            return []

        try:
            positions = await self.broker.get_positions()
        except Exception as e:
            logger.error("reconcile_get_positions_failed", error=str(e))
            return []

        live_pos_tickets = {p.ticket for p in positions}
        live_pending_tickets = {po.ticket for po in pending_orders}

        cancelled_tickets: list[int] = []

        from app.config.database import get_session
        from app.models.trade import Order as DBOrder
        from sqlalchemy import select

        for po in pending_orders:
            if po.ticket in self._cancelled_dual_entry_tickets:
                continue

            # 1. Find sibling tickets from in-memory cache first
            sibling_tickets: set[int] = set()

            sig_db_id = self._ticket_to_signal_db_id.get(po.ticket)
            if sig_db_id and sig_db_id in self._signal_db_id_to_tickets:
                for t in self._signal_db_id_to_tickets[sig_db_id]:
                    if t != po.ticket:
                        sibling_tickets.add(t)

            msg_id = self._ticket_to_msg_id.get(po.ticket)
            if msg_id and msg_id in self._signal_msg_id_to_tickets:
                for t in self._signal_msg_id_to_tickets[msg_id]:
                    if t != po.ticket:
                        sibling_tickets.add(t)

            if not sibling_tickets:
                for s_tickets in self._signal_db_id_to_tickets.values():
                    if po.ticket in s_tickets:
                        for t in s_tickets:
                            if t != po.ticket:
                                sibling_tickets.add(t)
                for m_tickets in self._signal_msg_id_to_tickets.values():
                    if po.ticket in m_tickets:
                        for t in m_tickets:
                            if t != po.ticket:
                                sibling_tickets.add(t)

            # 2. If not found in memory, query Database
            if not sibling_tickets:
                try:
                    async with get_session() as session:
                        stmt = select(DBOrder.signal_id).where(DBOrder.mt5_ticket == po.ticket)
                        signal_id = await session.scalar(stmt)
                        if signal_id:
                            stmt2 = select(DBOrder.mt5_ticket).where(
                                DBOrder.signal_id == signal_id,
                                DBOrder.mt5_ticket != po.ticket,
                                DBOrder.mt5_ticket.isnot(None),
                            )
                            db_siblings = (await session.scalars(stmt2)).all()
                            for t in db_siblings:
                                sibling_tickets.add(t)
                except Exception as e:
                    logger.debug("reconcile_db_lookup_error", error=str(e), ticket=po.ticket)

            if not sibling_tickets:
                continue

            # Check if any sibling was placed/filled, but is now neither in open positions nor pending orders
            # Meaning: that sibling order has closed!
            should_cancel = False
            closed_sibling_ticket = None
            for s_ticket in sibling_tickets:
                is_open = s_ticket in live_pos_tickets
                is_pending = s_ticket in live_pending_tickets
                if not is_open and not is_pending:
                    should_cancel = True
                    closed_sibling_ticket = s_ticket
                    break

            if should_cancel and closed_sibling_ticket:
                self._cancelled_dual_entry_tickets.add(po.ticket)
                try:
                    res = await self.broker.cancel_order(po.ticket)
                    if res.success:
                        cancelled_tickets.append(po.ticket)
                        await self._update_db_order_status(po.ticket, "CANCELLED")

                        msg_link = (
                            self._order_message_links.get(po.ticket)
                            or self._order_message_links.get(closed_sibling_ticket)
                        )
                        now_str = now_vietnam().strftime("%H:%M:%S %d/%m/%Y")
                        cancel_msg = (
                            f"🗑 <b>Hủy Lệnh Chờ Dual Entry</b>\n"
                            f"━━━━━━━━━━━━━━━━\n"
                            f"• <b>{po.symbol}</b> {po.order_type} <b>{po.volume} lot</b> @ <code>{po.price}</code>\n"
                            f"• Ticket đã hủy: <code>{po.ticket}</code>\n"
                            f"• Lý do: Lệnh song sinh (Ticket <code>{closed_sibling_ticket}</code>) đã đóng toàn bộ\n"
                            f"⏱ {now_str}"
                        )
                        await self.notify(self.format_notification(cancel_msg, msg_link))
                        logger.info(
                            "reconcile_dual_entry_pending_order_cancelled",
                            cancelled_ticket=po.ticket,
                            closed_ticket=closed_sibling_ticket,
                        )
                    else:
                        logger.warning(
                            "reconcile_dual_entry_cancel_failed",
                            ticket=po.ticket,
                            comment=res.comment,
                        )
                except Exception as e:
                    logger.error("reconcile_dual_entry_cancel_exception", ticket=po.ticket, error=str(e))

        return cancelled_tickets

    async def get_tickets_for_reply_message(self, reply_to_msg_id: int) -> list[int]:
        """Find MT5 tickets associated with the replied-to signal message ID."""
        tickets: list[int] = []
        if reply_to_msg_id in self._signal_msg_id_to_tickets:
            for t in self._signal_msg_id_to_tickets[reply_to_msg_id]:
                if t not in tickets:
                    tickets.append(t)
            if tickets:
                return tickets

        try:
            from sqlalchemy import select
            from app.config.database import get_session
            from app.models.signal import Signal as DBSignal
            from app.models.trade import Order as DBOrder

            async with get_session() as session:
                stmt = (
                    select(DBOrder.mt5_ticket)
                    .join(DBSignal, DBOrder.signal_id == DBSignal.id)
                    .where(DBSignal.telegram_message_id == reply_to_msg_id)
                    .where(DBOrder.mt5_ticket.isnot(None))
                )
                db_tickets = list((await session.scalars(stmt)).all())
                for t in db_tickets:
                    if t not in tickets:
                        tickets.append(t)
                if tickets:
                    self._signal_msg_id_to_tickets[reply_to_msg_id] = tickets
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

        return RiskContext(
            config=self.risk_config,
            account=account,
            open_positions=positions,
            recent_signals=self._recent_signals,
            broker_connected=connected,
        )

    async def process_raw_message(
        self,
        raw_text: str,
        is_reentry: bool = False,
        message_id: int | None = None,
        chat_id: int | None = None,
        message_link: str | None = None,
        reply_to_msg_id: int | None = None,
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
                    parser_version="1.0.0",
                )
                session.add(sig_record)
                await session.flush()
                signal_db_id = sig_record.id

                if signal_db_id and message_link:
                    self._signal_message_links[signal_db_id] = message_link

                if signal.new_order:
                    meta = {"message_link": message_link} if message_link else None
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
                        extra_metadata=meta,
                    )
                    session.add(parsed_record)
        except Exception as e:
            logger.warning("db_signal_record_failed", error=str(e))

        if signal.message_type == MessageType.UNKNOWN:
            logger.info("signal_unknown_skipped", text=raw_text[:60])
            return

        context = await self.build_risk_context()

        # 2. New Order Flow
        if signal.message_type == MessageType.NEW_ORDER and signal.new_order:
            action = signal.new_order.action
            symbol = signal.new_order.symbol

            # Fetch live current tick
            current_price = None
            try:
                tick = await self.broker.get_tick(symbol)
                if tick is not None:
                    p = tick.bid if action == OrderAction.SELL else tick.ask
                    if isinstance(p, (Decimal, int, float, str)):
                        current_price = Decimal(str(p))
            except Exception as e:
                logger.warning("fetch_tick_failed", symbol=symbol, error=str(e))

            if current_price is None or not isinstance(current_price, Decimal):
                current_price = signal.new_order.entry_min or Decimal("0")

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
                )
            if max_entry is not None:
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
                )

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
                    self._pending_overrides[sig_id] = {"orders": pending_reqs, "message_link": message_link}
                    kb = build_override_keyboard(sig_id, has_dual=has_dual_entry)
                    await self.notify(self.format_notification(warning_msg, message_link), reply_markup=kb)
                    return

                msg = (
                    f"⚠️ <b>Signal Rejected</b>\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"• Lệnh: <b>{signal.new_order.action.value} {signal.new_order.symbol}</b>\n"
                    f"• Lý do: {decision.rejection_reason}"
                )
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
                    self._pending_overrides[sig_id] = {"orders": pending_reqs, "message_link": message_link}
                    kb = build_override_keyboard(sig_id)
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
                    self._pending_overrides[sig_id] = {"orders": pending_reqs, "message_link": message_link}
                    kb = build_override_keyboard(sig_id)
                    await self.notify(self.format_notification(warning_msg, message_link), reply_markup=kb)
                    return

            # --- Order 1: Min Entry (Market order with favorable / slippage check) ---
            slippage = Decimal("0")
            is_favorable = True
            if min_entry is not None and current_price is not None:
                if action == OrderAction.SELL:
                    if current_price < min_entry:
                        is_favorable = False
                        slippage = min_entry - current_price
                else:
                    if current_price > min_entry:
                        is_favorable = False
                        slippage = current_price - min_entry

            if not is_favorable and slippage > self.risk_config.max_slippage_entry:
                warning_msg = (
                    f"⚠️ <b>Cảnh báo Min Entry: Vượt ngưỡng trượt giá</b>\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"• Cặp: <b>{symbol}</b> ({action.value})\n"
                    f"• Min Entry: <code>{min_entry}</code> | Hiện tại: <code>{current_price}</code>\n"
                    f"• Trượt bất lợi: <b>{slippage:.2f} giá</b> (Tối đa: {self.risk_config.max_slippage_entry} giá)\n\n"
                    f"➡️ <i>Tạm dừng đặt lệnh tự động để bạn quyết định!</i>"
                )
                from app.telegram.keyboards import build_override_keyboard
                self._pending_overrides[sig_id] = {
                    "orders": pending_reqs,
                    "message_link": message_link,
                    "message_id": message_id,
                }
                kb = build_override_keyboard(sig_id)
                await self.notify(self.format_notification(warning_msg, message_link), reply_markup=kb)
                return

            order_req_1 = OrderRequest(
                symbol=symbol,
                order_type=action.value,
                volume=vol_min,
                price=current_price,
                sl=signal.new_order.stop_loss,
                tp=signal.new_order.take_profit_2 or signal.new_order.take_profit,
                tp1=signal.new_order.take_profit,
                tp2=signal.new_order.take_profit_2,
                comment="bot-min-entry" if has_dual_entry else "bot-market",
            )
            result_1 = await self.broker.open_order(order_req_1)

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
                    status="FILLED",
                    message_link=message_link,
                    telegram_message_id=message_id,
                )
                entry_label = "(Min Entry)" if has_dual_entry else ""
                entry_tag = f" {entry_label}" if entry_label else ""
                now_str = now_vietnam().strftime("%H:%M:%S %d/%m/%Y")
                msg_1 = (
                    f"✅ <b>Order Executed{entry_tag}</b>\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"• <b>{order_req_1.symbol}</b> {order_req_1.order_type}\n"
                    f"• Khối lượng: <b>{result_1.volume or vol_min} lot</b> @ <code>{result_1.price or current_price}</code>\n"
                    f"• SL: <code>{order_req_1.sl or '—'}</code> | TP: <code>{order_req_1.tp or '—'}</code>\n"
                    f"• Ticket: <code>{result_1.ticket}</code>\n"
                    f"⏱ {now_str}"
                )
            else:
                msg_1 = (
                    f"❌ <b>Order Execution Failed {'(Min Entry)' if has_dual_entry else ''}</b>\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"• Lệnh: {order_req_1.order_type} {order_req_1.symbol}\n"
                    f"• Lý do: {result_1.comment} (Mã lỗi: {result_1.retcode})"
                )
            await self.notify(self.format_notification(msg_1, message_link))

            # --- Order 2: Max Entry (Pending Limit - always placed when dual entry is enabled) ---
            if has_dual_entry and max_entry is not None:
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
                    )
                    result_2 = await self.broker.open_order(order_req_2)

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
                        msg_2 = (
                            f"✅ <b>Order Placed (Max Entry)</b>\n"
                            f"━━━━━━━━━━━━━━━━\n"
                            f"• <b>{symbol}</b> {order2_type}\n"
                            f"• Khối lượng: <b>{result_2.volume or vol_max} lot</b> @ <code>{order2_price}</code>\n"
                            f"• SL: <code>{order_req_2.sl or '—'}</code> | TP: <code>{order_req_2.tp or '—'}</code>\n"
                            f"• Ticket: <code>{result_2.ticket}</code>\n"
                            f"⏱ {now_str}"
                        )
                    else:
                        msg_2 = (
                            f"❌ <b>Order Failed (Max Entry)</b>\n"
                            f"━━━━━━━━━━━━━━━━\n"
                            f"• Lệnh: {order2_type} {symbol}\n"
                            f"• Lý do: {result_2.comment} (Mã lỗi: {result_2.retcode})"
                        )
                    await self.notify(self.format_notification(msg_2, message_link))

            return

        # 3. Position Management Flow (chốt bớt, chốt, dời sl)
        if signal.message_type == MessageType.POSITION_MANAGEMENT:
            target_tickets: list[int] = []
            if reply_to_msg_id is not None:
                target_tickets = await self.get_tickets_for_reply_message(reply_to_msg_id)
                logger.info(
                    "reply_management_resolved",
                    reply_to_id=reply_to_msg_id,
                    target_tickets=target_tickets,
                )

            # Retrieve active broker positions & pending orders
            all_positions = await self.broker.get_positions()
            all_pending = await self.broker.get_pending_orders()

            is_reply_managed = False
            pending_to_cancel = []

            if reply_to_msg_id is not None:
                if target_tickets:
                    positions = [p for p in all_positions if p.ticket in target_tickets]
                    pending_to_cancel = [o for o in all_pending if o.ticket in target_tickets]
                    is_reply_managed = True
                else:
                    # reply_to_msg_id provided but no mapped tickets in DB
                    if signal.symbol:
                        positions = [
                            p for p in all_positions
                            if p.symbol.upper().startswith(signal.symbol.upper())
                        ]
                    else:
                        no_match_msg = (
                            f"⚠️ <b>Position Management:</b>\n"
                            f"━━━━━━━━━━━━━━━━\n"
                            f"Nhận tín hiệu reply Signal #{reply_to_msg_id}, "
                            f"nhưng không tìm thấy mã lệnh tương ứng trong hệ thống."
                        )
                        await self.notify(self.format_notification(no_match_msg, message_link))
                        return
            else:
                if signal.symbol:
                    positions = [
                        p for p in all_positions
                        if p.symbol.upper().startswith(signal.symbol.upper())
                    ]
                else:
                    positions = all_positions

            # Check if any positions or pending orders found
            if not positions and not pending_to_cancel:
                if is_reply_managed and target_tickets:
                    tickets_str = ", ".join(f"<code>{t}</code>" for t in target_tickets)
                    no_pos_msg = (
                        f"ℹ️ <b>Position Management:</b>\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"Nhận tín hiệu reply Signal #{reply_to_msg_id}, "
                        f"nhưng các lệnh (Ticket: {tickets_str}) đã được đóng trước đó."
                    )
                else:
                    no_pos_msg = (
                        f"ℹ️ <b>Position Management:</b>\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"Nhận tín hiệu cho {signal.symbol or 'tất cả'}, nhưng không có vị thế nào đang mở."
                    )
                await self.notify(self.format_notification(no_pos_msg, message_link))
                return

            # Cancel any pending limit orders of this replied signal if action is CLOSE
            for action in signal.management_actions:
                if action.action_type.value in ("CLOSE", "CANCEL"):
                    for po in pending_to_cancel:
                        res = await self.broker.cancel_order(po.ticket)
                        c_icon = "✅" if res.success else "❌"
                        now_str = now_vietnam().strftime("%H:%M:%S %d/%m/%Y")
                        cancel_msg = (
                            f"{c_icon} <b>Position Management: Hủy Lệnh Chờ Limit</b>\n"
                            f"━━━━━━━━━━━━━━━━\n"
                            f"• Áp dụng cho: <b>Signal #{reply_to_msg_id}</b>\n"
                            f"• Ticket: <code>{po.ticket}</code> ({po.symbol})\n"
                            f"• Kết quả: {res.comment}\n"
                            f"⏱ {now_str}"
                        )
                        await self.notify(self.format_notification(cancel_msg, message_link))

            if not positions:
                return

            # Calculate VWAP (Volume-Weighted Average Price) of ONLY the target positions
            total_vol = sum(p.volume for p in positions)
            vwap_price = Decimal("0")
            if total_vol > 0:
                vwap_price = sum(p.open_price * p.volume for p in positions) / total_vol
                vwap_price = vwap_price.quantize(Decimal("0.00001"))

            reply_tag = f"• Áp dụng cho: <b>Signal #{reply_to_msg_id}</b>\n" if is_reply_managed else ""

            pos_tickets = [p.ticket for p in positions]
            db_orders: dict[int, Any] = {}
            already_partial_closed_tickets: set[int] = set(self._tp1_processed_tickets)

            try:
                import inspect
                from app.config.database import get_session
                from app.models.trade import Order as DBOrder, ManagementAction
                from sqlalchemy import select

                async with get_session() as session:
                    if pos_tickets:
                        stmt_ord = select(DBOrder).where(DBOrder.mt5_ticket.in_(pos_tickets))
                        res_ord = await session.scalars(stmt_ord)
                        items_ord = res_ord.all()
                        if inspect.isawaitable(items_ord):
                            items_ord = await items_ord
                        db_orders = {o.mt5_ticket: o for o in items_ord if hasattr(o, "mt5_ticket")}

                        # Check executed partial closes in DB
                        stmt_act = select(ManagementAction.mt5_ticket).where(
                            ManagementAction.mt5_ticket.in_(pos_tickets),
                            ManagementAction.action_type == "PARTIAL_CLOSE",
                            ManagementAction.status == "EXECUTED",
                        )
                        res_act = await session.scalars(stmt_act)
                        items_act = res_act.all()
                        if inspect.isawaitable(items_act):
                            items_act = await items_act
                        for t in items_act:
                            already_partial_closed_tickets.add(t)
            except Exception as e:
                logger.debug("management_db_order_lookup_failed", error=str(e))

            for action in signal.management_actions:
                for pos in positions:
                    now_str = now_vietnam().strftime("%H:%M:%S %d/%m/%Y")
                    if action.action_type.value in ("CLOSE", "PARTIAL_CLOSE"):
                        vol = pos.volume
                        if action.action_type.value == "PARTIAL_CLOSE":
                            # Check if position was already partially closed (e.g. TP1 milestone reached)
                            db_order = db_orders.get(pos.ticket)
                            is_already_partial = (
                                not self.risk_config.allow_multiple_partial_closes
                                and (
                                    pos.ticket in already_partial_closed_tickets
                                    or (db_order and db_order.volume is not None and pos.volume < db_order.volume)
                                )
                            )
                            if is_already_partial:
                                skip_msg = (
                                    f"ℹ️ <b>Position Management: Bỏ qua chốt bớt</b>\n"
                                    f"━━━━━━━━━━━━━━━━\n"
                                    f"{reply_tag}"
                                    f"• <b>{pos.symbol}</b> (Ticket: <code>{pos.ticket}</code>)\n"
                                    f"• Vị thế đã chốt 1 phần trước đó (còn lại: <b>{pos.volume} lot</b>).\n"
                                    f"• Đã bỏ qua lệnh chốt thêm để bảo toàn volume gồng TP2.\n"
                                    f"⏱ {now_str}"
                                )
                                await self.notify(self.format_notification(skip_msg, message_link))
                                await self._insert_management_action(
                                    signal_db_id=signal_db_id,
                                    mt5_ticket=pos.ticket,
                                    action_type="PARTIAL_CLOSE",
                                    parameters={"volume": float(pos.volume), "reply_to_msg_id": reply_to_msg_id, "skipped": True, "reason": "ALREADY_PARTIALLY_CLOSED"},
                                    status="SKIPPED",
                                    result={"comment": "Skipped duplicate partial close (already executed)", "retcode": 0}
                                )

                                # Auto Breakeven on partial close if enabled and SL not at entry yet
                                if self.risk_config.auto_breakeven_on_partial:
                                    be_sl = vwap_price + self.risk_config.breakeven_offset_price
                                    if pos.sl is None or abs(pos.sl - be_sl) > Decimal("0.01"):
                                        be_res = await self.broker.modify_position(pos.ticket, sl=be_sl)
                                        be_icon = "✅" if be_res.success else "⚠️"
                                        be_msg = (
                                            f"{be_icon} <b>Auto Breakeven on Partial</b>\n"
                                            f"━━━━━━━━━━━━━━━━\n"
                                            f"{reply_tag}"
                                            f"• Ticket: <code>{pos.ticket}</code> | SL mới: <code>{be_sl}</code>\n"
                                            f"• Kết quả: {be_res.comment}\n"
                                            f"⏱ {now_str}"
                                        )
                                        await self.notify(self.format_notification(be_msg, message_link))
                                        await self._insert_management_action(
                                            signal_db_id=signal_db_id,
                                            mt5_ticket=pos.ticket,
                                            action_type="AUTO_BREAKEVEN",
                                            parameters={"sl": float(be_sl), "reply_to_msg_id": reply_to_msg_id},
                                            status="EXECUTED" if be_res.success else "FAILED",
                                            result={"comment": be_res.comment, "retcode": be_res.retcode}
                                        )
                                continue

                            raw_pct = action.percentage or self.risk_config.default_partial_close_percent
                            pct = raw_pct / Decimal("100")
                            vol = (pos.volume * pct).quantize(Decimal("0.01"))
                            vol = max(Decimal("0.01"), min(vol, pos.volume))

                        res = await self.broker.close_position(pos.ticket, vol)
                        if res.success and action.action_type.value == "PARTIAL_CLOSE":
                            self._tp1_processed_tickets.add(pos.ticket)
                            already_partial_closed_tickets.add(pos.ticket)
                        status_icon = "✅" if res.success else "❌"
                        action_name = f"Chốt {action.percentage or 50}%" if action.action_type.value == "PARTIAL_CLOSE" else "Đóng Lệnh"
                        close_msg = (
                            f"{status_icon} <b>Position Management: {action_name}</b>\n"
                            f"━━━━━━━━━━━━━━━━\n"
                            f"{reply_tag}"
                            f"• <b>{pos.symbol}</b> (Ticket: <code>{pos.ticket}</code>)\n"
                            f"• Khối lượng: <b>{vol} lot</b> | Kết quả: {res.comment}\n"
                            f"⏱ {now_str}"
                        )
                        await self.notify(self.format_notification(close_msg, message_link))
                        if res.success and action.action_type.value == "CLOSE":
                            await self.cancel_dual_entry_pending_orders_on_close(
                                pos.ticket, reason="Tín hiệu Đóng lệnh"
                            )
                        
                        await self._insert_management_action(
                            signal_db_id=signal_db_id,
                            mt5_ticket=pos.ticket,
                            action_type=action.action_type.value,
                            parameters={"volume": float(vol), "reply_to_msg_id": reply_to_msg_id},
                            status="EXECUTED" if res.success else "FAILED",
                            result={"comment": res.comment, "retcode": res.retcode}
                        )

                        # Auto Breakeven on partial close if enabled
                        if (
                            res.success
                            and action.action_type.value == "PARTIAL_CLOSE"
                            and self.risk_config.auto_breakeven_on_partial
                        ):
                            be_sl = vwap_price + self.risk_config.breakeven_offset_price
                            be_res = await self.broker.modify_position(pos.ticket, sl=be_sl)
                            be_icon = "✅" if be_res.success else "⚠️"
                            be_msg = (
                                f"{be_icon} <b>Auto Breakeven on Partial</b>\n"
                                f"━━━━━━━━━━━━━━━━\n"
                                f"{reply_tag}"
                                f"• Ticket: <code>{pos.ticket}</code> | SL mới: <code>{be_sl}</code>\n"
                                f"• Kết quả: {be_res.comment}\n"
                                f"⏱ {now_str}"
                            )
                            await self.notify(self.format_notification(be_msg, message_link))
                            await self._insert_management_action(
                                signal_db_id=signal_db_id,
                                mt5_ticket=pos.ticket,
                                action_type="AUTO_BREAKEVEN",
                                parameters={"sl": float(be_sl), "reply_to_msg_id": reply_to_msg_id},
                                status="EXECUTED" if be_res.success else "FAILED",
                                result={"comment": be_res.comment, "retcode": be_res.retcode}
                            )

                    elif action.action_type.value in ("MOVE_SL", "BREAKEVEN"):
                        new_sl = vwap_price + self.risk_config.breakeven_offset_price
                        res = await self.broker.modify_position(pos.ticket, sl=new_sl)
                        status_icon = "✅" if res.success else "❌"
                        move_msg = (
                            f"{status_icon} <b>Position Management: Dời SL về Entry Hòa Vốn</b>\n"
                            f"━━━━━━━━━━━━━━━━\n"
                            f"{reply_tag}"
                            f"• Ticket: <code>{pos.ticket}</code> ({pos.symbol})\n"
                            f"• SL mới: <code>{new_sl}</code> (Hòa vốn)\n"
                            f"• Kết quả: {res.comment}\n"
                            f"⏱ {now_str}"
                        )
                        await self.notify(self.format_notification(move_msg, message_link))
                        await self._insert_management_action(
                            signal_db_id=signal_db_id,
                            mt5_ticket=pos.ticket,
                            action_type=action.action_type.value,
                            parameters={"sl": float(new_sl), "reply_to_msg_id": reply_to_msg_id},
                            status="EXECUTED" if res.success else "FAILED",
                            result={"comment": res.comment, "retcode": res.retcode}
                        )

    # -------------------------------------------------------------
    # Admin Control Operations (called by Personal Telegram Bot)
    # -------------------------------------------------------------

    async def get_status_summary(self) -> dict[str, Any]:
        """Fetch system and account status."""
        acc = await self.broker.get_account_info()
        positions = await self.broker.get_positions()
        orders = await self.broker.get_pending_orders()
        connected = await self.broker.is_connected()
        return {
            "bot_enabled": self.risk_config.enabled,
            "connected": connected,
            "login": acc.login,
            "server": acc.server,
            "balance": acc.balance,
            "equity": acc.equity,
            "margin": acc.margin,
            "free_margin": acc.free_margin,
            "open_positions_count": len(positions),
            "pending_orders_count": len(orders),
            "currency": acc.currency,
        }

    async def close_position_by_ticket(
        self, ticket: int, volume: Decimal | None = None
    ) -> OrderResult:
        """Close an individual position."""
        res = await self.broker.close_position(ticket, volume)
        if res.success:
            await self.cancel_dual_entry_pending_orders_on_close(ticket, reason="Đóng thủ công / Admin")
        return res

    async def close_all_positions(self) -> list[OrderResult]:
        """Emergency or manual close all open positions."""
        positions = await self.broker.get_positions()
        results: list[OrderResult] = []
        for pos in positions:
            res = await self.broker.close_position(pos.ticket, pos.volume)
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
        return await self.broker.modify_position(ticket, sl, tp)

    async def move_to_breakeven(self, ticket: int) -> OrderResult:
        """Move position stop loss to middle (VWAP) entry price of all positions for the symbol."""
        positions = await self.broker.get_positions()
        target = next((p for p in positions if p.ticket == ticket), None)
        if not target:
            return OrderResult(success=False, comment=f"Ticket {ticket} not found")
            
        symbol_positions = [p for p in positions if p.symbol == target.symbol]
        total_vol = sum(p.volume for p in symbol_positions)
        vwap_price = target.open_price
        if total_vol > 0:
            vwap_price = sum(p.open_price * p.volume for p in symbol_positions) / total_vol
            vwap_price = vwap_price.quantize(Decimal("0.00001"))
            
        new_sl = vwap_price + self.risk_config.breakeven_offset_price
        return await self.broker.modify_position(ticket, sl=new_sl)

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

    async def get_closed_positions(
        self, days: int = 7, limit: int | None = 15
    ) -> list[ClosedPositionInfo]:
        """
        Retrieve closed positions aggregated across all partial exit deals.

        A single position can be closed in multiple partial steps (e.g. TP1, TP2, manual close).
        This method groups deals by position_id and computes volume-weighted average exit prices,
        aggregate profit, swap, commission, net PnL, and original position type (BUY/SELL).
        """
        try:
            deals = await self.broker.get_history_deals(days=days)
        except Exception as e:
            logger.warning("get_closed_positions_history_failed", error=str(e))
            return []

        try:
            active_positions = await self.broker.get_positions()
            active_tickets = {p.ticket for p in active_positions}
            active_pos_map = {p.ticket: p for p in active_positions}
        except Exception as e:
            logger.warning("get_closed_positions_active_failed", error=str(e))
            active_tickets = set()
            active_pos_map = {}

        currency = "USD"
        try:
            acc = await self.broker.get_account_info()
            if acc and acc.currency:
                currency = acc.currency
        except Exception:
            pass

        from collections import defaultdict

        import re

        grouped_deals: dict[int, list[DealInfo]] = defaultdict(list)
        for d in deals:
            # Skip balance/deposit records and invalid deals
            if not d.symbol or d.deal_type not in ("BUY", "SELL"):
                continue
            pos_id = d.position_id
            if pos_id <= 0 and d.comment:
                match = re.search(r"#?(\d{6,})", d.comment)
                if match:
                    pos_id = int(match.group(1))
            if pos_id <= 0:
                pos_id = d.order if d.order > 0 else d.ticket
            if pos_id <= 0:
                continue
            grouped_deals[pos_id].append(d)

        missing_entry_pos_ids: list[int] = []
        aggregated: list[dict[str, Any]] = []

        for pos_id, p_deals in grouped_deals.items():
            out_deals = [
                d
                for d in p_deals
                if d.entry in ("OUT", "INOUT", "OUT_BY")
                or (d.profit != Decimal("0") and d.entry != "IN")
            ]
            if not out_deals:
                continue

            in_deals = [d for d in p_deals if d.entry == "IN"]

            total_vol = sum((d.volume for d in out_deals), Decimal("0"))
            if total_vol <= Decimal("0"):
                continue

            # Weighted average close price: sum(p * v) / total_vol
            weighted_close_sum = sum((d.price * d.volume for d in out_deals), Decimal("0"))
            close_price = (weighted_close_sum / total_vol).quantize(Decimal("0.00001"))

            profit = sum((d.profit for d in out_deals), Decimal("0"))
            swap = sum((d.swap for d in p_deals), Decimal("0"))
            commission = sum((d.commission for d in p_deals), Decimal("0"))
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
            is_fully_closed = pos_id not in active_tickets
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
                            if pid in db_positions and db_positions[pid].entry_price is not None:
                                item["open_price"] = db_positions[pid].entry_price
                                item["open_time"] = db_positions[pid].created_at
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
                comment=item["comment"],
            )
            for item in aggregated
        ]

        result.sort(key=lambda p: p.close_time, reverse=True)
        if limit is not None and limit > 0:
            return result[:limit]
        return result

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
                    for cfg in configs:
                        val = cfg.value
                        if isinstance(val, dict):
                            for k, v in val.items():
                                if hasattr(self.risk_config, k):
                                    self.risk_config.update_field(k, v)
                        elif hasattr(self.risk_config, cfg.key):
                            self.risk_config.update_field(cfg.key, val)

                    logger.info("loaded_risk_config_from_db", enabled=self.risk_config.enabled)
                    return self.risk_config
        except Exception as e:
            logger.warning("load_risk_config_from_db_failed", error=str(e))
        return self.risk_config

    async def save_risk_config_to_db(self, updated_by: str = "SYSTEM") -> bool:
        """Persist current risk_config state to PostgreSQL bot_config table."""
        try:
            from sqlalchemy import select
            from app.config.database import get_session
            from app.models.config import BotConfig

            grouped_configs = {
                ("trading", "general"): {
                    "enabled": bool(self.risk_config.enabled),
                    "allowed_symbols": list(self.risk_config.allowed_symbols),
                    "trading_hours": "ALL",
                },
                ("risk", "limits"): {
                    "max_open_positions": int(self.risk_config.max_open_positions),
                    "max_lot": float(self.risk_config.max_lot),
                    "min_lot": float(self.risk_config.min_lot),
                    "default_lot": float(self.risk_config.default_lot),
                    "max_daily_loss_percent": float(self.risk_config.max_daily_loss_percent),
                    "require_sl": bool(self.risk_config.require_sl),
                    "require_tp": bool(self.risk_config.require_tp),
                },
                ("risk", "dual_entry"): {
                    "enable_dual_entry": bool(self.risk_config.enable_dual_entry),
                    "lot_min_entry": float(self.risk_config.lot_min_entry),
                    "lot_max_entry": float(self.risk_config.lot_max_entry),
                    "max_slippage_entry": float(self.risk_config.max_slippage_entry),
                },
                ("risk", "position_management"): {
                    "default_partial_close_percent": float(self.risk_config.default_partial_close_percent),
                    "auto_breakeven_on_partial": bool(self.risk_config.auto_breakeven_on_partial),
                    "breakeven_offset_price": float(self.risk_config.breakeven_offset_price),
                    "allow_multiple_partial_closes": bool(self.risk_config.allow_multiple_partial_closes),
                },
                ("signal", "timing"): {
                    "duplicate_timeout_seconds": int(self.risk_config.duplicate_timeout_seconds),
                    "signal_expire_seconds": int(self.risk_config.signal_expire_seconds),
                    "broker_utc_offset_hours": int(self.risk_config.broker_utc_offset_hours),
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
        """Update a config field in memory and persist immediately to database."""
        if not self.risk_config.update_field(key, value):
            return False
        return await self.save_risk_config_to_db(updated_by=updated_by)

    async def update_config_keys(
        self, updates: dict[str, Any], updated_by: str = "ADMIN"
    ) -> dict[str, bool]:
        """Update multiple config fields in memory and persist once to database."""
        results: dict[str, bool] = {}
        any_success = False
        for k, v in updates.items():
            ok = self.risk_config.update_field(k, v)
            results[k] = ok
            if ok:
                any_success = True
        if any_success:
            await self.save_risk_config_to_db(updated_by=updated_by)
        return results

    async def add_or_update_management_rule(
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

        # 1. Update in memory
        self.parser.rule_engine.add_or_update_rule(
            keyword=clean_kw,
            action_type=action_type,
            params=params,
            priority=priority,
        )

        # 2. Persist to PostgreSQL
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
            return True
        except Exception as e:
            logger.error("save_management_rule_db_failed", keyword=clean_kw, error=str(e))
            return True

    async def remove_management_rule(self, keyword: str) -> bool:
        """Remove a rule from memory and database."""
        clean_kw = keyword.lower().strip()
        mem_ok = self.parser.rule_engine.remove_rule(clean_kw)

        try:
            from sqlalchemy import delete
            from app.config.database import get_session
            from app.models.config import ManagementRule

            async with get_session() as session:
                stmt = delete(ManagementRule).where(ManagementRule.keyword == clean_kw)
                await session.execute(stmt)
        except Exception as e:
            logger.error("delete_management_rule_db_failed", keyword=clean_kw, error=str(e))

        return mem_ok


# Global singleton instance
trading_service = TradingService()
