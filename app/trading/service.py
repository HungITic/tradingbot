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
                res = self._notify_callback(message, **kwargs)
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
                            await self.notify(
                                f"🔔 <b>Lệnh chờ đã khớp (Pending Order Filled)</b>\n"
                                f"Cặp: <b>{deal.symbol}</b> | {deal.deal_type}\n"
                                f"Khối lượng: {deal.volume} lot @ {deal.price}\n"
                                f"Ticket Deal: <code>{deal.ticket}</code> (Order: {deal.order})"
                            )
                        
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
                            await self.notify(
                                f"{icon} <b>Vị thế đã đóng ({reason_str})</b>\n"
                                f"Cặp: <b>{deal.symbol}</b> | {deal.deal_type}\n"
                                f"Khối lượng: {deal.volume} lot @ {deal.price}\n"
                                f"Lợi nhuận: <b>{deal.profit}$</b>"
                            )
                
                _is_first_sync = False
            except Exception as e:
                logger.error("sync_loop_error", error=str(e))
                
            await asyncio.sleep(10)

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
        reqs = self._pending_overrides.get(signal_db_id)
        if not reqs:
            return "❌ Lệnh chờ không còn hiệu lực hoặc đã quá hạn."
        
        targets_to_execute = list(reqs.keys()) if target == "both" else [target]
        results = []
        
        for t in targets_to_execute:
            req = reqs.pop(t, None)
            if not req:
                results.append(f"❌ Không tìm thấy lệnh chờ {t}.")
                continue
                
            result = await self.broker.open_order(req)
            if result.success:
                await self._record_db_order(signal_db_id, result, req, status="PLACED")
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
    ) -> None:
        """Persist executed or placed order into PostgreSQL audit trail."""
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
    ) -> None:
        """Full pipeline: Raw message -> Parse -> Risk -> Execute -> Notify."""
        logger.info("processing_signal_message", text=raw_text[:80])

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

                if signal.new_order:
                    parsed_record = DBParsedSignal(
                        signal_id=signal_db_id,
                        action=signal.new_order.action.value,
                        symbol=signal.new_order.symbol,
                        entry_min=signal.new_order.entry_min,
                        entry_max=signal.new_order.entry_max,
                        stop_loss=signal.new_order.stop_loss,
                        take_profit=signal.new_order.take_profit,
                        raw_tp_text=signal.new_order.raw_tp_text,
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
                await self.notify(msg)
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
                    tp=signal.new_order.take_profit,
                    comment="bot-override-min",
                )
            if max_entry is not None:
                pending_reqs["max"] = OrderRequest(
                    symbol=symbol,
                    order_type=_get_pending_type(action, current_price, max_entry),
                    volume=vol_max,
                    price=max_entry,
                    sl=signal.new_order.stop_loss,
                    tp=signal.new_order.take_profit,
                    comment="bot-override-max",
                )
            
            # Check if price has completely exceeded the entire range (e.g. SELL and price > max_entry)
            if has_dual_entry and max_entry is not None:
                if action == OrderAction.SELL and current_price > max_entry:
                    warning_msg = (
                        f"⚠️ <b>Cảnh báo: Giá hiện tại vượt quá Max Entry</b>\n"
                        f"• Cặp: <b>{symbol}</b> (SELL)\n"
                        f"• Vùng Entry: <code>{min_entry} - {max_entry}</code> | Hiện tại: <code>{current_price}</code>\n"
                        f"➡️ <i>Bỏ qua tín hiệu vì giá thị trường đã vượt quá toàn bộ vùng Entry!</i>"
                    )
                    from app.telegram.keyboards import build_override_keyboard
                    self._pending_overrides[sig_id] = pending_reqs
                    kb = build_override_keyboard(sig_id)
                    await self.notify(warning_msg, reply_markup=kb)
                    return
                elif action == OrderAction.BUY and current_price < min(min_entry, max_entry):
                    warning_msg = (
                        f"⚠️ <b>Cảnh báo: Giá hiện tại vượt quá vùng Entry</b>\n"
                        f"• Cặp: <b>{symbol}</b> (BUY)\n"
                        f"• Vùng Entry: <code>{min_entry} - {max_entry}</code> | Hiện tại: <code>{current_price}</code>\n"
                        f"➡️ <i>Bỏ qua tín hiệu vì giá thị trường đã vượt quá toàn bộ vùng Entry!</i>"
                    )
                    from app.telegram.keyboards import build_override_keyboard
                    self._pending_overrides[sig_id] = pending_reqs
                    kb = build_override_keyboard(sig_id)
                    await self.notify(warning_msg, reply_markup=kb)
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
                self._pending_overrides[sig_id] = pending_reqs
                kb = build_override_keyboard(sig_id)
                await self.notify(warning_msg, reply_markup=kb)
                return

            # order1_allowed logic is removed since we return early
            order_req_1 = OrderRequest(
                symbol=symbol,
                order_type=action.value,
                volume=vol_min,
                price=current_price,
                sl=signal.new_order.stop_loss,
                tp=signal.new_order.take_profit,
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
                await self._record_db_order(signal_db_id, result_1, order_req_1, status="FILLED")
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
            await self.notify(msg_1)

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
                        tp=signal.new_order.take_profit,
                        comment="bot-max-entry",
                    )
                    result_2 = await self.broker.open_order(order_req_2)

                    if result_2.success:
                        await self._record_db_order(
                            signal_db_id,
                            result_2,
                            order_req_2,
                            status="PLACED",
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
                    await self.notify(msg_2)

            return

        # 3. Position Management Flow (chốt bớt, chốt, dời sl)
        if signal.message_type == MessageType.POSITION_MANAGEMENT:
            positions = await self.broker.get_positions(signal.symbol)
            if not positions:
                await self.notify(
                    f"ℹ️ Received management signal for {signal.symbol or 'all'}, but no open positions found."
                )
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
                        await self.notify(
                            f"{status_icon} <b>Position Management: Close</b>\n"
                            f"Ticket: <code>{pos.ticket}</code> | Vol: {vol} lot\n"
                            f"Result: {res.comment}"
                        )
                        
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
                            await self.notify(
                                f"{be_icon} <b>Auto Breakeven on Partial</b>\n"
                                f"Ticket: <code>{pos.ticket}</code> | SL (Middle): {be_sl}\n"
                                f"Result: {be_res.comment}"
                            )
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
                        await self.notify(
                            f"{status_icon} <b>Position Management: Move SL to Middle Entry</b>\n"
                            f"Ticket: <code>{pos.ticket}</code> | New SL: {new_sl}\n"
                            f"Result: {res.comment}"
                        )
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
