"""Read-only previews and execution checks shared by Telegram and the listener."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from html import escape
from typing import Any

from app.telegram.presentation import heading, section, field, price as display_price, number, DIVIDER
from app.broker.base import OrderRequest, OrderResult
from app.core.timezone import now_vietnam, to_vietnam_datetime
from app.signal.models import MessageType, OrderAction
from app.signal.validator import SignalValidator


class TradingSafetyMixin:
    def adjusted_signal_levels(self, signal):
        """Derive execution levels from the original signal, in absolute price units."""
        if not signal.new_order:
            return signal
        order = signal.new_order
        offset = Decimal("-0.5") if order.action == OrderAction.BUY else Decimal("0.5")
        levels = {}
        for name in ("stop_loss", "take_profit", "take_profit_2"):
            value = getattr(order, name)
            levels[name] = value + offset if value is not None else None
        return signal.model_copy(update={"new_order": order.model_copy(update=levels)})

    def signal_problem(self, signal, signal_time: datetime | None = None, *, skip_signal_expiry_check: bool = False) -> str | None:
        if signal_time is not None:
            if signal_time.tzinfo is None:
                return "Thời gian tín hiệu phải có múi giờ."
            age = (datetime.now(timezone.utc) - signal_time.astimezone(timezone.utc)).total_seconds()
            if age < -30:
                return "Thời gian tín hiệu nằm trong tương lai; cần kiểm tra đồng hồ."
            if not skip_signal_expiry_check and age > self.risk_config.signal_expire_seconds:
                return f"Tín hiệu đã hết hạn ({int(age)} giây; giới hạn {self.risk_config.signal_expire_seconds} giây)."
        if signal.management_actions and self.risk_config.require_explicit_close:
            ambiguous = {"chốt nha", "chốt nhé", "chốt đi", "chốt lời", "dừng"}
            if any(a.action_type.value == "CLOSE" and a.keyword in ambiguous for a in signal.management_actions):
                return "Từ khóa đóng lệnh chưa rõ phạm vi; hãy dùng 'chốt hết', 'đóng lệnh' hoặc chọn ticket."
        validator = SignalValidator(self.risk_config.require_sl, self.risk_config.require_tp)
        valid, reason = validator.validate_signal(signal)
        return None if valid else reason

    async def daily_risk_snapshot(self, account) -> tuple[Decimal, Decimal]:
        """Use all account deals, including fees and balance operations, on the VN day."""
        now = now_vietnam()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        deals = await self.broker.get_history_deals(start_time=start)
        today = [d for d in deals if start <= to_vietnam_datetime(d.time) <= now]
        pnl = sum((d.profit + d.swap + d.commission for d in today
                   if d.deal_type in ("BUY", "SELL")), Decimal("0"))
        # Include deposits/withdrawals to reconstruct the actual opening balance.
        movement = sum((d.profit + d.swap + d.commission for d in today), Decimal("0"))
        return pnl, account.balance - movement

    async def checked_tick(self, symbol: str):
        tick = await self.broker.get_tick(symbol)
        if tick is None or not isinstance(tick.time, datetime):
            raise ValueError("Không lấy được giá thị trường có thời gian hợp lệ")
        age = (now_vietnam() - to_vietnam_datetime(tick.time)).total_seconds()
        if age > 60 or age < -30:
            raise ValueError("Giá thị trường đã cũ hoặc đồng hồ không đồng bộ")
        if not tick.bid.is_finite() or not tick.ask.is_finite() or tick.bid <= 0 or tick.ask < tick.bid:
            raise ValueError("Giá bid/ask không hợp lệ")
        return tick

    def check_request(self, req: OrderRequest) -> str | None:
        from app.signal.models import ParsedNewOrder
        side = OrderAction.BUY if req.order_type.startswith("BUY") else OrderAction.SELL
        order = ParsedNewOrder(action=side, symbol=req.symbol, raw_symbol=req.symbol,
                               entry_min=req.price, stop_loss=req.sl,
                               take_profit=req.tp1 or req.tp, take_profit_2=req.tp2)
        valid, reason = SignalValidator(self.risk_config.require_sl, self.risk_config.require_tp).validate_new_order(order)
        if not valid:
            return reason
        if (not req.volume.is_finite() or not self.risk_config.min_lot <= req.volume <= self.risk_config.max_lot):
            return "Khối lượng nằm ngoài giới hạn lot đã cấu hình"
        return None

    async def preview_signal(self, raw_text: str, signal_time: datetime | None = None,
                             is_reentry: bool = False, skip_signal_expiry_check: bool = False, **kwargs: Any) -> dict:
        """Does not write the DB, send notifications, or execute broker operations."""
        signal = self.parser.parse(raw_text)
        signal.is_reentry = is_reentry
        signal = self.adjusted_signal_levels(signal)
        problem = self.signal_problem(signal, signal_time, skip_signal_expiry_check=skip_signal_expiry_check)
        lines = [heading("🔎 Xem trước tín hiệu", "Chưa thực thi giao dịch"), ""]
        origin = kwargs.get('reentry_origin')
        if is_reentry and isinstance(origin, dict):
            from app.telegram.utils import construct_telegram_message_link
            link = construct_telegram_message_link(origin.get('chat_id'), origin.get('root_message_id'))
            if link:
                lines.append(f'Vòng mới từ <a href="{escape(link, quote=True)}">signal gốc</a> · '
                             f'{max(0, len(origin.get("message_path", [])) - 1)} bước reply')
        if signal_time:
            lines.append(f"Tin gốc: {escape(to_vietnam_datetime(signal_time).strftime('%H:%M:%S %d/%m/%Y'))}")
        can_execute = False
        management_scope = None
        if signal.message_type == MessageType.UNKNOWN:
            lines.append("Không xác định được hành động rõ ràng. Tin mơ hồ/điều kiện cần được làm rõ.")
        elif signal.new_order:
            order = signal.new_order
            dual = (self.risk_config.enable_dual_entry and order.entry_max is not None
                    and order.entry_max != order.entry_min)
            volumes = ([self.risk_config.lot_min_entry, self.risk_config.lot_max_entry] if dual
                       else [self.risk_config.default_lot])
            lines += ["", heading(f"{order.symbol} · {order.action.value}"), "",
                section("VÙNG VÀO LỆNH", field("Entry thứ nhất", display_price(order.entry_min), code=True),
                    field("Entry thứ hai", display_price(order.entry_max or order.entry_min), code=True)), "",
                section("BẢO VỆ LỆNH", field("SL · Dừng lỗ", display_price(order.stop_loss), code=True),
                    field("TP1", display_price(order.take_profit), code=True), field("TP2", display_price(order.take_profit_2), code=True)), "",
                "SL/TP đã bù 0.5 giá từ tín hiệu: BUY trừ 0.5 · SELL cộng 0.5.", "",
                section("KHỐI LƯỢNG DỰ KIẾN", field("Số lệnh tối đa", len(volumes) * self.native_leg_count(order.take_profit, order.take_profit_2)),
                    field("Tổng khối lượng", f"{number(sum(volumes))} lot")), ""]
            if not problem:
                context = await self.build_risk_context()
                from app.trading.position_policy import PartialVolumeTooSmall
                for pos in await self.opposite_signal_positions(order):
                    try:
                        amount = await self.partial_volume(pos, Decimal('50'))
                        lines.append(f'Signal ngược chiều: chốt 50% {pos.symbol} {pos.position_type} '
                                     f'còn lại ({number(pos.volume)} → đóng {number(amount)} lot).')
                    except PartialVolumeTooSmall:
                        lines.append(f'{pos.symbol} {pos.position_type}: lot còn lại quá nhỏ để chốt 50%; giữ nguyên.')
                context.planned_order_count = len(volumes) * self.native_leg_count(order.take_profit, order.take_profit_2)
                decision = self.risk_engine.evaluate(signal, context)
                if not decision.approved:
                    problem = decision.rejection_reason
            if not problem:
                try:
                    tick = await self.checked_tick(order.symbol)
                    price = tick.ask if order.action == OrderAction.BUY else tick.bid
                    lines.append(field("Giá hiện tại", number(price, minimum=2), code=True))
                    adverse = (price - order.entry_min if order.action == OrderAction.BUY else order.entry_min - price) if order.entry_min is not None else 0
                    use_limit = adverse > self.risk_config.max_slippage_entry
                    req = OrderRequest(order.symbol, order.action.value + '_LIMIT' if use_limit else order.action.value,
                                       volumes[0], order.entry_min if use_limit else price,
                                       order.stop_loss, order.take_profit_2 or order.take_profit,
                                       order.take_profit, order.take_profit_2, comment='bot-market')
                    problem = self.check_request(req)
                    if use_limit:
                        lines.append(f'Trượt giá {number(adverse)}: tự đặt {req.order_type} tại {display_price(req.price)}, không chờ duyệt trượt giá.')
                    for volume in volumes:
                        from dataclasses import replace
                        legs = await self.prepare_native_orders(replace(req, volume=volume))
                        if len(legs) > 1:
                            lines.append('Tách TP trên MT5: ' + ' · '.join(f'{number(r.volume)} lot → {display_price(r.tp)}' for r in legs))
                except Exception as exc:
                    problem = str(exc)
            can_execute = not problem
            lines.extend(["", DIVIDER, "", self.management_policy_description(), ""])
            lines.append("Khi thực thi, bot kiểm tra lại giá. Trượt bất lợi quá ngưỡng tự đặt limit tại entry; các giới hạn rủi ro khác vẫn có thể yêu cầu duyệt.")
        else:
            lines.append("Quản lý vị thế: " + ", ".join(a.action_type.value for a in signal.management_actions))
            reply_id = kwargs.get("reply_to_msg_id")
            reply_id = await self.management_reply_id(kwargs.get('chat_id'), reply_id, kwargs.get('related_message_path'))
            positions = await self.get_open_positions()
            pending_scope = []
            try:
                management_scope = await self.resolve_management_scope(signal, reply_id, kwargs.get('chat_id'),
                    kwargs.get('message_id'), kwargs.get('management_scope'))
                if management_scope:
                    reply_id = management_scope['message_id']
                    from app.telegram.utils import construct_telegram_message_link
                    source_link = construct_telegram_message_link(management_scope['chat_id'], reply_id)
                    lines.append(f'Không có reply: áp dụng <a href="{source_link}">signal mở lệnh gần nhất</a> trong cùng chat.')
            except Exception as exc:
                problem = problem or str(exc)
                positions = []
            if reply_id is not None:
                if management_scope:
                    tickets = management_scope['tickets']
                elif kwargs.get('chat_id') is not None:
                    tickets = await self.get_tickets_for_reply_message(reply_id, chat_id=kwargs['chat_id'])
                else:
                    tickets = await self.get_tickets_for_reply_message(reply_id)
                positions = [p for p in positions if self.branch_has_ticket(p, tickets)]
                if any(a.action_type.value == 'CLOSE' for a in signal.management_actions):
                    pending_scope = [o for o in await self.broker.get_pending_orders() if o.ticket in tickets]
                lines.append("Phạm vi reply: " + (", ".join(map(str, tickets)) or "không tìm thấy vị thế"))
                problem = problem or (None if tickets else "Không xác định được kèo đích")
            elif signal.symbol:
                positions = [p for p in positions if p.symbol.upper().startswith(signal.symbol.upper())]
            else:
                import re
                if not re.search(r"(?i)tất cả|toàn bộ|all", signal.raw_message):
                    if any(a.target == "MIN_ENTRY" for a in signal.management_actions):
                        try:
                            positions = await self.unique_bad_entry_scope(signal, positions)
                        except Exception as exc:
                            problem = problem or str(exc)
                    else:
                        problem = problem or "Cần symbol, reply đúng kèo hoặc chỉ rõ 'tất cả'"
            lines.append(f"Phạm vi: {len(positions)} vị thế theo bộ lọc {escape(signal.symbol or 'reply/toàn tài khoản')}")
            if pending_scope:
                lines.append(f'Có {len(pending_scope)} lệnh chờ thuộc kèo trong phạm vi đóng.')
            limit_cancellations = []
            if not problem:
                try:
                    limit_cancellations = await self.preview_slippage_cancellations(signal, kwargs.get('chat_id'),
                        kwargs.get('message_id'), reply_id, kwargs.get('related_message_path'), management_scope)
                    if limit_cancellations:
                        lines.append(f'Action này sẽ hủy {len(limit_cancellations)} limit trượt giá chưa khớp của kèo.')
                except Exception as exc:
                    problem = str(exc)
            problem = problem or (None if positions or pending_scope or limit_cancellations else "Không có vị thế phù hợp")
            if any(a.target == "MIN_ENTRY" for a in signal.management_actions) and positions:
                try:
                    orders = await self.position_orders(positions)
                    groups = await self.position_groups(positions, orders)
                    for action in signal.management_actions:
                        if action.target == "MIN_ENTRY":
                            selected = self.management_selection(action, positions, orders, groups, reply_id, signal.raw_message)
                            lines.append("Chỉ chốt Min Entry: " + ", ".join(f"#{p.ticket}" for p in selected) + "; giữ nguyên Max Entry.")
                except Exception as exc:
                    problem = problem or str(exc)
            lines.extend(["", DIVIDER, "", self.management_policy_description(), ""])
            for action in signal.management_actions:
                if action.action_type.value == "PARTIAL_CLOSE":
                    lines.append(f"Tỷ lệ chốt thực thi: {self.partial_percentage(action)}%")
                    if self.partial_percentage(action) not in {0, 100}:
                        lines.append('Nếu vị thế đã chốt một phần: ' + ('được phép chốt tiếp theo cấu hình/từ khóa.'
                                     if self.repeat_partial_allowed(signal.raw_message) else 'bỏ qua vì tin này chưa được phép chốt tiếp.'))
                if action.price is not None:
                    lines.append(f"SL đích được yêu cầu: {action.price}")
            can_execute = not problem
        if problem:
            lines.extend(["", section("⛔ CHƯA ĐỦ ĐIỀU KIỆN", escape(problem))])
        else:
            lines.extend(["", section("✅ KẾT QUẢ KIỂM TRA", "Đủ điều kiện để gửi vào luồng kiểm tra thực thi.")])
        result = {"text": "\n".join(lines), "can_execute": can_execute}
        if management_scope:
            result['management_scope'] = {k: v for k, v in management_scope.items() if k != 'tickets'}
        return result

    async def emergency_stop(self) -> dict:
        """Pause first, serialize with signal execution, cancel pending orders before closing."""
        self.risk_config.enabled = False
        async with self._execution_lock:
            # A config transaction already in flight may have just published a candidate.
            # Reassert and persist the pause after obtaining the execution lane.
            self.risk_config.enabled = False
            saved = await self.save_risk_config_to_db(updated_by="KILL_SWITCH")
            self._pending_overrides.clear()
            cancellations, closes = [], []
            errors = []
            try:
                orders = await self.broker.get_pending_orders()
                for order in orders:
                    try:
                        cancellations.append(await self.broker.cancel_order(order.ticket))
                    except Exception as exc:
                        cancellations.append(OrderResult(False, ticket=order.ticket, comment=str(exc)))
            except Exception as exc:
                errors.append(f"Không đọc được lệnh chờ: {exc}")
            try:
                positions = await self.broker.get_positions()
                for pos in positions:
                    try:
                        result = await self.broker.close_position(pos.ticket, pos.volume)
                        closes.append(result)
                        await self.record_management(None, pos, "CLOSE", result, reason="KILL_SWITCH")
                    except Exception as exc:
                        closes.append(OrderResult(False, ticket=pos.ticket, comment=str(exc)))
            except Exception as exc:
                errors.append(f"Không đọc được vị thế: {exc}")
            try:
                remaining_positions = len(await self.broker.get_positions())
                remaining_orders = len(await self.broker.get_pending_orders())
            except Exception as exc:
                remaining_positions = remaining_orders = None
                errors.append(f"Chưa đối soát được trạng thái sau dừng: {exc}")
            return {"saved": saved, "closes": closes, "cancellations": cancellations,
                    "remaining_positions": remaining_positions, "remaining_orders": remaining_orders,
                    "errors": errors}

    async def set_signal_status(self, signal_id: int | None, status: str, reason: str = "") -> None:
        if signal_id is None:
            return
        from sqlalchemy import update
        from app.config.database import get_session
        from app.models.signal import Signal
        from app.models.risk import RiskEvent
        async with get_session() as session:
            await session.execute(update(Signal).where(Signal.id == signal_id).values(status=status))
            if reason:
                session.add(RiskEvent(signal_id=signal_id, event_type=status, rule_name="pipeline",
                                      details={"reason": reason}))

    async def get_signal_inbox(self, page: int = 0, status: str = "ALL") -> list:
        from sqlalchemy import select
        from app.config.database import get_session
        from app.models.signal import Signal
        from app.models.risk import RiskEvent
        reason = (select(RiskEvent.details).where(RiskEvent.signal_id == Signal.id)
                  .order_by(RiskEvent.id.desc()).limit(1).correlate(Signal).scalar_subquery())
        stmt = select(Signal, reason.label("reason")).order_by(Signal.id.desc()).offset(page * 5).limit(6)
        if status != "ALL":
            stmt = stmt.where(Signal.status == status)
        async with get_session() as session:
            return list((await session.execute(stmt)).all())

    async def get_setup_cards(self) -> list[dict]:
        """Group live broker state by the recorded signal; never infer a group by symbol."""
        from sqlalchemy import select
        from app.config.database import get_session
        from app.models.trade import Order
        positions = await self.get_open_positions()
        orders = await self.broker.get_pending_orders()
        tickets = [x.ticket for x in positions + orders]
        memberships = {}
        if tickets:
            async with get_session() as session:
                rows = await session.execute(select(Order.mt5_ticket, Order.signal_id)
                                             .where(Order.mt5_ticket.in_(tickets)))
                memberships = dict(rows.all())
        groups = {}
        for item in positions + orders:
            sig_id = memberships.get(item.ticket)
            key = f"Kèo #{sig_id}" if sig_id else f"Chưa liên kết · #{item.ticket}"
            group = groups.setdefault(key, {"name": key, "positions": [], "orders": []})
            group["positions" if hasattr(item, "profit") else "orders"].append(item)
        return list(groups.values())
