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
    DealInfo,
    OrderRequest,
    OrderResult,
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
)

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
        
        while True:
            try:
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
                            filled_msg = (
                                f"🔔 <b>Lệnh chờ đã khớp (Pending Order Filled)</b>\n"
                                f"Cặp: <b>{deal.symbol}</b> | {deal.deal_type}\n"
                                f"Khối lượng: {deal.volume} lot @ {deal.price}\n"
                                f"Ticket Deal: <code>{deal.ticket}</code> (Order: {deal.order})"
                            )
                            await self.notify(self.format_notification(filled_msg, order_link))
                        
                    elif deal.entry in ("OUT", "OUT_BY", "INOUT"):
                        # Position closed (SL, TP or manual)
                        inserted = await self._insert_trade_record(deal)
                        if inserted and not _is_first_sync:
                            if deal.reason == 3:
                                reason_str = "Dính SL (StopLoss)"
                            elif deal.reason == 4:
                                reason_str = "Chốt lời TP (TakeProfit)"
                            else:
                                reason_str = "Đóng lệnh"
                                
                            icon = "🔴" if deal.profit < 0 else "🟢"
                            deal_link = self._order_message_links.get(deal.order) or self._order_message_links.get(deal.ticket)
                            closed_msg = (
                                f"{icon} <b>Vị thế đã đóng ({reason_str})</b>\n"
                                f"Cặp: <b>{deal.symbol}</b> | {deal.deal_type}\n"
                                f"Khối lượng: {deal.volume} lot @ {deal.price}\n"
                                f"Lợi nhuận: <b>{deal.profit}$</b>"
                            )
                            await self.notify(self.format_notification(closed_msg, deal_link))
                
                _is_first_sync = False

                # Evaluate TP1 and TP2 milestones for open positions
                await self._check_tp_milestones()
            except Exception as e:
                logger.error("sync_loop_error", error=str(e))
                
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
                    tp2_msg = (
                        f"{status_icon} <b>Đạt TP2 ({tp2}): Chốt lời Full</b>\n"
                        f"Ticket: <code>{pos.ticket}</code> | Cặp: <b>{pos.symbol}</b> ({pos.position_type})\n"
                        f"Khối lượng: {pos.volume} lot @ {current_price}\n"
                        f"Kết quả: {res.comment}"
                    )
                    await self.notify(self.format_notification(tp2_msg, pos_link))
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
                        tp1_be_msg = (
                            f"{be_icon} <b>Đạt TP1 ({tp1}): Dời SL về Entry</b>\n"
                            f"Ticket: <code>{pos.ticket}</code> | Cặp: <b>{pos.symbol}</b>\n"
                            f"<i>Volume {pos.volume} lot tối thiểu không thể chia nhỏ, dời SL về {pos.open_price} để gồng tiếp TP2!</i>"
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
                        tp1_part_msg = (
                            f"{icon} <b>Đạt TP1 ({tp1}): Chốt 50% & Dời SL về Entry</b>\n"
                            f"Ticket: <code>{pos.ticket}</code> | Cặp: <b>{pos.symbol}</b>\n"
                            f"Đã chốt: <b>{vol_to_close} lot</b> | Còn lại: <b>{rem_vol} lot</b>\n"
                            f"SL mới: <code>{pos.open_price}</code> (Entry hòa vốn)"
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
        
        if isinstance(entry, dict) and "orders" in entry:
            reqs = entry["orders"]
            msg_link = entry.get("message_link")
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
                await self._record_db_order(signal_db_id, result, req, status="PLACED", message_link=msg_link)
                results.append(
                    f"✅ <b>Order Placed ({t.capitalize()} Entry Override)</b>\n"
                    f"Ticket: <code>{result.ticket}</code> | Volume: {result.volume or req.volume} lot @ {req.price}"
                )
            else:
                results.append(
                    f"❌ <b>Order Failed ({t.capitalize()} Entry Override)</b>\n"
                    f"Retcode: {result.retcode} ({result.comment})"
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
    ) -> None:
        """Persist executed or placed order into PostgreSQL audit trail."""
        if result.ticket and message_link:
            self._order_message_links[result.ticket] = message_link
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
    ) -> None:
        """Full pipeline: Raw message -> Parse -> Risk -> Execute -> Notify."""
        logger.info("processing_signal_message", text=raw_text[:80], message_link=message_link)

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
            decision = self.risk_engine.evaluate(signal, context)

            if not decision.approved:
                msg = (
                    f"⚠️ <b>Signal Rejected</b>\n"
                    f"Action: {signal.new_order.action.value} {signal.new_order.symbol}\n"
                    f"Reason: {decision.rejection_reason}"
                )
                await self.notify(self.format_notification(msg, message_link))
                return

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
            vol_min = self.risk_config.lot_min_entry if has_dual_entry else decision.calculated_lot
            vol_max = self.risk_config.lot_max_entry

            if min_entry is not None:
                pending_reqs["min"] = OrderRequest(
                    symbol=symbol,
                    order_type=_get_pending_type(action, current_price, min_entry),
                    volume=vol_min,
                    price=min_entry,
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
            
            # Check if price has completely exceeded the entire range (e.g. SELL and price > max_entry, or BUY and price < max_entry)
            if has_dual_entry and max_entry is not None:
                if action == OrderAction.SELL and current_price > max_entry:
                    warning_msg = (
                        f"⚠️ <b>Cảnh báo: Giá hiện tại vượt quá Max Entry</b>\n"
                        f"• Cặp: <b>{symbol}</b> (SELL)\n"
                        f"• Vùng Entry: <code>{min_entry} - {max_entry}</code> | Hiện tại: <code>{current_price}</code>\n"
                        f"➡️ <i>Bỏ qua tín hiệu vì giá thị trường đã vượt quá toàn bộ vùng Entry!</i>"
                    )
                    from app.telegram.keyboards import build_override_keyboard
                    self._pending_overrides[sig_id] = {"orders": pending_reqs, "message_link": message_link}
                    kb = build_override_keyboard(sig_id)
                    await self.notify(self.format_notification(warning_msg, message_link), reply_markup=kb)
                    return
                elif action == OrderAction.BUY and current_price < max_entry:
                    warning_msg = (
                        f"⚠️ <b>Cảnh báo: Giá hiện tại vượt quá Max Entry</b>\n"
                        f"• Cặp: <b>{symbol}</b> (BUY)\n"
                        f"• Vùng Entry: <code>{min_entry} - {max_entry}</code> | Hiện tại: <code>{current_price}</code>\n"
                        f"➡️ <i>Bỏ qua tín hiệu vì giá thị trường đã vượt quá toàn bộ vùng Entry!</i>"
                    )
                    from app.telegram.keyboards import build_override_keyboard
                    self._pending_overrides[sig_id] = {"orders": pending_reqs, "message_link": message_link}
                    kb = build_override_keyboard(sig_id)
                    await self.notify(self.format_notification(warning_msg, message_link), reply_markup=kb)
                    return

            # Check if current price is in the middle zone (khoảng giữa) or beyond towards max_entry
            is_mid_or_beyond = False
            if has_dual_entry and min_entry is not None and max_entry is not None:
                mid_entry = (min_entry + max_entry) / Decimal("2")
                range_span = abs(max_entry - min_entry)
                tol = min(Decimal("1.0"), range_span * Decimal("0.25"))
                if action == OrderAction.SELL:
                    if current_price >= (mid_entry - tol):
                        is_mid_or_beyond = True
                else:  # BUY
                    if current_price <= (mid_entry + tol):
                        is_mid_or_beyond = True

            # --- Order 1: Min Entry / Mid Entry (Market order with favorable / slippage check) ---
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
                    f"• Cặp: <b>{symbol}</b> ({action.value})\n"
                    f"• Min Entry: <code>{min_entry}</code> | Hiện tại: <code>{current_price}</code>\n"
                    f"• Trượt bất lợi: <b>{slippage:.2f} giá</b> (Tối đa cho phép: {self.risk_config.max_slippage_entry} giá)\n"
                    f"➡️ <i>Bỏ qua tín hiệu (tạm dừng đặt lệnh tự động) để bạn quyết định!</i>"
                )
                from app.telegram.keyboards import build_override_keyboard
                self._pending_overrides[sig_id] = {"orders": pending_reqs, "message_link": message_link}
                kb = build_override_keyboard(sig_id)
                await self.notify(self.format_notification(warning_msg, message_link), reply_markup=kb)
                return

            # order1_allowed logic is removed since we return early
            order_req_1 = OrderRequest(
                symbol=symbol,
                order_type=action.value,
                volume=vol_min,
                price=current_price,
                sl=signal.new_order.stop_loss,
                tp=signal.new_order.take_profit_2 or signal.new_order.take_profit,
                tp1=signal.new_order.take_profit,
                tp2=signal.new_order.take_profit_2,
                comment="bot-mid-entry" if is_mid_or_beyond else "bot-min-entry",
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
                await self._record_db_order(signal_db_id, result_1, order_req_1, status="FILLED", message_link=message_link)
                entry_label = "(Mid Entry)" if is_mid_or_beyond else ("(Min Entry)" if has_dual_entry else "")
                entry_tag = f" {entry_label}" if entry_label else ""
                msg_1 = (
                    f"✅ <b>Order Executed{entry_tag}</b>\n"
                    f"Type: {order_req_1.order_type} {order_req_1.symbol}\n"
                    f"Ticket: <code>{result_1.ticket}</code>\n"
                    f"Volume: {result_1.volume or vol_min} lot @ {result_1.price or current_price}\n"
                    f"SL: {order_req_1.sl or 'None'} | TP: {order_req_1.tp or 'None'}"
                )
                if is_mid_or_beyond:
                    msg_1 += (
                        f"\nℹ️ <i>Giá hiện tại ({current_price}) đã nằm ở khoảng giữa vùng Entry ({min_entry} - {max_entry}) "
                        f"➔ Chỉ đặt 1 lệnh Market, không cài Limit Max Entry.</i>"
                    )
            else:
                msg_1 = (
                    f"❌ <b>Order Execution Failed {'(Min Entry)' if has_dual_entry else ''}</b>\n"
                    f"Type: {order_req_1.order_type} {order_req_1.symbol}\n"
                    f"Retcode: {result_1.retcode} ({result_1.comment})"
                )
            await self.notify(self.format_notification(msg_1, message_link))

            # --- Order 2: Max Entry (Pending Limit ONLY - skipped if price in mid zone or reached max) ---
            if has_dual_entry and max_entry is not None and not is_mid_or_beyond:
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
                            signal_db_id,
                            result_2,
                            order_req_2,
                            status="PLACED",
                            message_link=message_link,
                        )
                        msg_2 = (
                            f"✅ <b>Order Placed (Max Entry)</b>\n"
                            f"Type: {order2_type} {symbol}\n"
                            f"Ticket: <code>{result_2.ticket}</code>\n"
                            f"Volume: {result_2.volume or vol_max} lot @ {order2_price}\n"
                            f"SL: {order_req_2.sl or 'None'} | TP: {order_req_2.tp or 'None'}"
                        )
                    else:
                        msg_2 = (
                            f"❌ <b>Order Failed (Max Entry)</b>\n"
                            f"Type: {order2_type} {symbol}\n"
                            f"Retcode: {result_2.retcode} ({result_2.comment})"
                        )
                    await self.notify(self.format_notification(msg_2, message_link))

            return

        # 3. Position Management Flow (chốt bớt, chốt, dời sl)
        if signal.message_type == MessageType.POSITION_MANAGEMENT:
            positions = await self.broker.get_positions(signal.symbol)
            if not positions:
                no_pos_msg = f"ℹ️ Received management signal for {signal.symbol or 'all'}, but no open positions found."
                await self.notify(self.format_notification(no_pos_msg, message_link))
                return

            # Calculate VWAP (Volume-Weighted Average Price) for Breakeven
            total_vol = sum(p.volume for p in positions)
            vwap_price = Decimal("0")
            if total_vol > 0:
                vwap_price = sum(p.open_price * p.volume for p in positions) / total_vol
                # Round to 5 decimal places (standard max for Forex)
                vwap_price = vwap_price.quantize(Decimal("0.00001"))

            for action in signal.management_actions:
                for pos in positions:
                    if action.action_type.value in ("CLOSE", "PARTIAL_CLOSE"):
                        vol = pos.volume
                        if action.action_type.value == "PARTIAL_CLOSE":
                            raw_pct = action.percentage or self.risk_config.default_partial_close_percent
                            pct = raw_pct / Decimal("100")
                            vol = (pos.volume * pct).quantize(Decimal("0.01"))
                            vol = max(Decimal("0.01"), min(vol, pos.volume))

                        res = await self.broker.close_position(pos.ticket, vol)
                        status_icon = "✅" if res.success else "❌"
                        close_msg = (
                            f"{status_icon} <b>Position Management: Close</b>\n"
                            f"Ticket: <code>{pos.ticket}</code> | Vol: {vol} lot\n"
                            f"Result: {res.comment}"
                        )
                        await self.notify(self.format_notification(close_msg, message_link))
                        
                        await self._insert_management_action(
                            signal_db_id=signal_db_id,
                            mt5_ticket=pos.ticket,
                            action_type=action.action_type.value,
                            parameters={"volume": float(vol)},
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
                                f"Ticket: <code>{pos.ticket}</code> | SL (Middle): {be_sl}\n"
                                f"Result: {be_res.comment}"
                            )
                            await self.notify(self.format_notification(be_msg, message_link))
                            await self._insert_management_action(
                                signal_db_id=signal_db_id,
                                mt5_ticket=pos.ticket,
                                action_type="AUTO_BREAKEVEN",
                                parameters={"sl": float(be_sl)},
                                status="EXECUTED" if be_res.success else "FAILED",
                                result={"comment": be_res.comment, "retcode": be_res.retcode}
                            )

                    elif action.action_type.value in ("MOVE_SL", "BREAKEVEN"):
                        # Target VWAP ENTRY
                        new_sl = vwap_price + self.risk_config.breakeven_offset_price
                        res = await self.broker.modify_position(pos.ticket, sl=new_sl)
                        status_icon = "✅" if res.success else "❌"
                        move_msg = (
                            f"{status_icon} <b>Position Management: Move SL to Middle Entry</b>\n"
                            f"Ticket: <code>{pos.ticket}</code> | New SL: {new_sl}\n"
                            f"Result: {res.comment}"
                        )
                        await self.notify(self.format_notification(move_msg, message_link))
                        await self._insert_management_action(
                            signal_db_id=signal_db_id,
                            mt5_ticket=pos.ticket,
                            action_type=action.action_type.value,
                            parameters={"sl": float(new_sl)},
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
        return await self.broker.close_position(ticket, volume)

    async def close_all_positions(self) -> list[OrderResult]:
        """Emergency or manual close all open positions."""
        positions = await self.broker.get_positions()
        results: list[OrderResult] = []
        for pos in positions:
            res = await self.broker.close_position(pos.ticket, pos.volume)
            results.append(res)
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
        """Retrieve recent closed trade deals (exit executions) from broker."""
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

    async def get_performance_summary(self, days: int = 7) -> dict[str, Any]:
        """Compute performance metrics (win/loss, profit factor, net pnl) over the given days."""
        try:
            deals = await self.broker.get_history_deals(days=days)
            closed = [
                d
                for d in deals
                if d.symbol
                and d.deal_type in ("BUY", "SELL")
                and (d.entry in ("OUT", "INOUT", "OUT_BY") or d.profit != Decimal("0"))
            ]
        except Exception as e:
            logger.warning("get_performance_deals_failed", error=str(e))
            closed = []

        total_trades = len(closed)
        wins = [d for d in closed if d.profit > Decimal("0")]
        losses = [d for d in closed if d.profit < Decimal("0")]
        breakevens = [d for d in closed if d.profit == Decimal("0")]

        total_profit = sum((d.profit for d in wins), Decimal("0"))
        total_loss = sum((d.profit for d in losses), Decimal("0"))  # negative Decimal
        total_swap = sum((d.swap for d in closed), Decimal("0"))
        total_commission = sum((d.commission for d in closed), Decimal("0"))
        net_pnl = total_profit + total_loss + total_swap + total_commission

        win_rate = (len(wins) / total_trades * 100) if total_trades > 0 else 0.0

        abs_loss = abs(total_loss)
        if abs_loss > Decimal("0"):
            profit_factor = float(total_profit / abs_loss)
        elif total_profit > Decimal("0"):
            profit_factor = 999.0
        else:
            profit_factor = 0.0

        best_trade = max((d.profit for d in closed), default=Decimal("0"))
        worst_trade = min((d.profit for d in closed), default=Decimal("0"))

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
                },
                ("signal", "timing"): {
                    "duplicate_timeout_seconds": int(self.risk_config.duplicate_timeout_seconds),
                    "signal_expire_seconds": int(self.risk_config.signal_expire_seconds),
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
