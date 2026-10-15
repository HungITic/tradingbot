"""One explicit management policy for signals, commands, buttons and TP milestones."""
from __future__ import annotations

import re
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from html import escape

import structlog
from sqlalchemy import or_, select

from app.broker.base import OrderResult, PositionInfo
from app.config import database
from app.models.trade import ManagementAction, Order, Position
from app.signal.models import ManagementType

logger = structlog.get_logger(__name__)


class PartialVolumeTooSmall(ValueError):
    pass


class PositionPolicyMixin:
    def management_policy_description(self) -> str:
        cfg = self.risk_config
        scope = "entry riêng từng ticket"
        return (f"<b>SL VÀ HÒA VỐN</b>\nHòa vốn theo giá, chưa tính phí/swap: {scope}; không tính trung bình hai entry.\n"
                f"Bù giá: BUY +{cfg.breakeven_offset_price}, SELL −{cfg.breakeven_offset_price}.\n"
                f"\n<b>CHỐT LỜI</b>\nTP1 khi có TP2: {'bật' if cfg.tp1_enabled else 'tắt'}, chốt {cfg.default_partial_close_percent}%.\n"
                f"Tự dời SL sau chốt: {'bật' if cfg.auto_breakeven_on_partial else 'tắt'}.\n"
                f"Thiếu lot tại TP1: {'chỉ dời SL nếu đã bật tự dời' if cfg.tp1_min_volume_action == 'breakeven' else 'bỏ qua'}.\n"
                "Chỉ có một TP: đóng toàn bộ tại TP đó.\n"
                "\n<b>QUẢN LÝ NHÁNH</b>\nChốt bớt + xấu: chỉ chốt Min Entry theo tỷ lệ, giữ Max Entry.\n"
                f"Chốt bớt chung dùng tỷ lệ cấu hình: {'bật' if cfg.generic_partial_uses_default else 'tắt'}.\n"
                f"Hủy nhánh chờ sau khi vị thế đóng hết: {'bật' if cfg.auto_cancel_pending_on_full_close else 'tắt'}.\n"
                f"Giữ SL đang bảo vệ tốt hơn: {'bật' if cfg.breakeven_only_improve else 'tắt'}.\n"
                "SL ban đầu lấy từ tín hiệu; không tự chuyển về giữa hai entry.")

    def partial_percentage(self, action):
        if action.target == "MIN_ENTRY" and action.percentage_source != "message":
            return self.risk_config.default_partial_close_percent
        generic = {"chốt bớt", "chốt bớt 1 phần", "chốt 1 phần", "chốt lãi 1 phần", "chốt bớt lãi"}
        if (self.risk_config.generic_partial_uses_default and action.keyword in generic
                and action.percentage_source != "message"):
            return self.risk_config.default_partial_close_percent
        return action.percentage if action.percentage is not None else self.risk_config.default_partial_close_percent

    async def position_orders(self, positions: list[PositionInfo]) -> dict:
        if not positions:
            return {}
        async with database.get_session() as session:
            rows = await session.scalars(select(Order).where(
                Order.mt5_ticket.in_([p.ticket for p in positions])))
            return {o.mt5_ticket: o for o in rows.all()}

    async def position_groups(self, positions: list[PositionInfo], orders: dict | None = None) -> dict:
        """Unknown membership stays isolated; symbol or direction alone never identifies a setup."""
        groups = {p.ticket: self._ticket_to_signal_db_id.get(p.ticket) for p in positions}
        for signal_id, tickets in self._signal_db_id_to_tickets.items():
            for ticket in tickets:
                if ticket in groups:
                    groups[ticket] = signal_id
        # Reply mappings are useful even before a DB position row exists.
        for message_id, tickets in self._signal_msg_id_to_tickets.items():
            for ticket in tickets:
                if ticket in groups and groups[ticket] is None:
                    groups[ticket] = ("message", message_id)
        if orders is None:
            try:
                orders = await self.position_orders(positions)
            except Exception as exc:
                logger.warning("position_membership_unavailable", error=str(exc))
                orders = {}
        for ticket, order in orders.items():
            if isinstance(order.signal_id, int) and order.signal_id > 0:
                groups[ticket] = order.signal_id
        return groups

    def breakeven_target(self, target: PositionInfo, positions: list[PositionInfo], groups: dict) -> Decimal:
        base = target.open_price
        offset = self.risk_config.breakeven_offset_price
        return base + offset if target.position_type == "BUY" else base - offset

    async def apply_stop(self, target: PositionInfo, price: Decimal, *, protect: bool = True) -> OrderResult:
        """Normalize to broker precision and validate against a fresh executable quote."""
        if not price.is_finite() or price <= 0:
            return OrderResult(False, ticket=target.ticket, comment="Giá SL không hợp lệ")
        try:
            spec = await self.broker.get_symbol_info(target.symbol)
            price = price.quantize(Decimal(1).scaleb(-spec.digits), rounding=ROUND_HALF_UP)
            if target.sl is not None and target.sl > 0:
                if price == target.sl:
                    return OrderResult(True, ticket=target.ticket, price=price, comment="SL đã ở mức yêu cầu", raw={"unchanged": True})
                worse = price < target.sl if target.position_type == "BUY" else price > target.sl
                if protect and self.risk_config.breakeven_only_improve and worse:
                    return OrderResult(True, ticket=target.ticket, price=target.sl,
                                       comment="Giữ SL hiện tại vì đang bảo vệ tốt hơn", raw={"unchanged": True})
            tick = await self.checked_tick(target.symbol)
            valid = price < tick.bid if target.position_type == "BUY" else price > tick.ask
            if not valid:
                return OrderResult(False, ticket=target.ticket, price=price, comment="SL không nằm đúng phía giá thị trường hiện tại")
            result = await self.broker.modify_position(target.ticket, sl=price)
            result.price = price
            return result
        except Exception as exc:
            return OrderResult(False, ticket=target.ticket, comment=f"Không kiểm tra/sửa được SL: {exc}")

    async def breakeven_preview(self, ticket: int) -> dict:
        positions = await self.broker.get_positions()
        target = next((p for p in positions if p.ticket == ticket), None)
        if target is None:
            raise ValueError("Không tìm thấy vị thế")
        spec = await self.broker.get_symbol_info(target.symbol)
        price = self.breakeven_target(target, positions, {}).quantize(
            Decimal(1).scaleb(-spec.digits), rounding=ROUND_HALF_UP)
        text = (f"🎯 <b>Xem trước hòa vốn · #{ticket}</b>\n"
                f"SL hiện tại: {target.sl or '—'}\nSL tính theo chính sách: {price}\n"
                "Chỉ sửa ticket được chọn; không sửa các ticket khác.\n"
                + self.management_policy_description())
        return {"text": text, "price": price}

    async def move_to_breakeven(self, ticket: int, expected_price: Decimal | None = None) -> OrderResult:
        async with self._execution_lock:
            positions = await self.broker.get_positions()
            target = next((p for p in positions if p.ticket == ticket), None)
            if target is None:
                return OrderResult(False, ticket=ticket, comment="Không tìm thấy vị thế")
            price = self.breakeven_target(target, positions, {})
            if expected_price is not None:
                spec = await self.broker.get_symbol_info(target.symbol)
                price = price.quantize(Decimal(1).scaleb(-spec.digits), rounding=ROUND_HALF_UP)
                if price != expected_price:
                    return OrderResult(False, ticket=ticket, comment="Giá hòa vốn đã thay đổi; hãy mở lại bản xem trước")
            return await self.apply_stop(target, price)

    async def partial_volume(self, pos: PositionInfo, percentage: Decimal) -> Decimal:
        pct = Decimal(str(percentage))
        if not pct.is_finite() or not 0 < pct < 100:
            raise ValueError("Tỷ lệ chốt một phần phải lớn hơn 0 và nhỏ hơn 100%")
        spec = await self.broker.get_symbol_info(pos.symbol)
        step, minimum = spec.volume_step, spec.volume_min
        if step <= 0 or minimum <= 0:
            raise ValueError("Thông số bước lot của broker không hợp lệ")
        volume = (pos.volume * pct / 100 / step).to_integral_value(rounding=ROUND_DOWN) * step
        if volume < minimum or pos.volume - volume < minimum:
            raise PartialVolumeTooSmall("Khối lượng quá nhỏ để chốt một phần; chưa đóng vị thế")
        return volume

    async def partial_one(self, pos: PositionInfo, percentage: Decimal) -> tuple[OrderResult, Decimal]:
        try:
            volume = await self.partial_volume(pos, percentage)
        except Exception as exc:
            return OrderResult(False, ticket=pos.ticket, comment=str(exc), raw={"not_divisible": isinstance(exc, PartialVolumeTooSmall)}), Decimal("0")
        result = await self.broker.close_position(pos.ticket, volume)
        return result, volume

    async def management_history(self, positions: list[PositionInfo]) -> tuple[set, set]:
        """Read audit by ticket in JSON, with a fallback for older Position-linked records."""
        tickets = [p.ticket for p in positions]
        partial, tp1_done = set(), set()
        if not tickets:
            return partial, tp1_done
        async with database.get_session() as session:
            rows = await session.execute(select(ManagementAction, Position.mt5_ticket)
                .outerjoin(Position, ManagementAction.position_id == Position.id)
                .where(or_(Position.mt5_ticket.in_(tickets),
                           ManagementAction.parameters["ticket"].astext.in_([str(t) for t in tickets]))))
            for action, legacy_ticket in rows.all():
                params = action.parameters or {}
                ticket = params.get("ticket") or legacy_ticket
                if action.status == "EXECUTED" and action.action_type == "PARTIAL_CLOSE":
                    partial.add(ticket)
                if params.get("reason") == "TP1_COMPLETE" and action.status in {"EXECUTED", "SKIPPED"}:
                    tp1_done.add(ticket)
        return partial, tp1_done

    async def record_management(self, signal_id, pos, action, result, **parameters):
        metadata = {}
        if action in {"CLOSE", "PARTIAL_CLOSE"}:
            cause = parameters.get("reason")
            if parameters.get("target") == "MIN_ENTRY" and action == "PARTIAL_CLOSE":
                cause = "MIN_ENTRY_PARTIAL"
            if cause not in {"TP1_PARTIAL", "FINAL_TP", "MANUAL_PARTIAL", "MANUAL_CLOSE", "CLOSE_ALL", "KILL_SWITCH", "MIN_ENTRY_PARTIAL"}:
                cause = "SIGNAL_PARTIAL" if action == "PARTIAL_CLOSE" else "SIGNAL_CLOSE"
            partial = (action == "PARTIAL_CLOSE" or result.retcode == 10010
                       or (result.volume is not None and 0 < result.volume < pos.volume))
            metadata = self.close_execution_metadata(pos.ticket, result, cause, partial=partial)
        await self._insert_management_action(signal_id, pos.ticket, action, parameters,
            "EXECUTED" if result.success else "FAILED",
            {**metadata, "comment": result.comment, "retcode": result.retcode})

    async def partial_close_positions(self, percentage=Decimal("50"), symbol=None, ticket=None):
        async with self._execution_lock:
            all_positions = await self.broker.get_positions()
            positions = all_positions
            if symbol:
                clean = symbol.upper().replace("#", "").strip()
                positions = [p for p in positions if p.symbol.upper().startswith(clean)]
            if ticket is not None:
                positions = [p for p in positions if p.ticket == ticket]
            return await self.partial_positions(positions, percentage, all_positions)

    async def partial_positions(self, positions, percentage, all_positions=None):
        """Only selected tickets are modified; setup averages include their live siblings."""
        all_positions = all_positions if all_positions is not None else await self.broker.get_positions()
        orders = await self.position_orders(all_positions)
        groups = await self.position_groups(all_positions, orders)
        history, _ = await self.management_history(positions)
        results = []
        for pos in positions:
            order = orders.get(pos.ticket)
            already = pos.ticket in history or pos.ticket in self._tp1_processed_tickets
            already |= bool(order and order.volume is not None and pos.volume < order.volume)
            if already and not self.risk_config.allow_multiple_partial_closes:
                result, volume = OrderResult(False, ticket=pos.ticket, comment="Vị thế đã chốt một phần; đã chặn chốt lặp"), Decimal("0")
            else:
                result, volume = await self.partial_one(pos, percentage)
                if result.success:
                    self._tp1_processed_tickets.add(pos.ticket)
                    group = groups.get(pos.ticket)
                    await self.record_management(group if isinstance(group, int) else None,
                                                 pos, "PARTIAL_CLOSE", result, volume=str(volume), reason="MANUAL_PARTIAL")
                    if self.risk_config.auto_breakeven_on_partial:
                        be = await self.apply_stop(pos, self.breakeven_target(pos, all_positions, groups))
                        await self.record_management(None, pos, "AUTO_BREAKEVEN", be, reason="MANUAL_PARTIAL", sl=str(be.price))
                        result.comment += (f" · SL: {be.price} ({be.comment})" if be.success
                                           else " · Chốt thành công nhưng chưa dời SL: " + be.comment)
            results.append((pos, result, volume))
        return results

    async def _check_tp_milestones(self):
        async with self._execution_lock:
            try:
                await self.check_tp_milestones()
            except Exception as exc:
                logger.exception("check_tp_milestones_failed", error=str(exc))

    async def check_tp_milestones(self):
        positions = await self.broker.get_positions()
        orders = await self.position_orders(positions)
        groups = await self.position_groups(positions, orders)
        previous_partial, completed = await self.management_history(positions)
        for pos in positions:
            order = orders.get(pos.ticket)
            if order is None:
                continue
            reached = lambda price: price is not None and (pos.current_price >= price if pos.position_type == "BUY" else pos.current_price <= price)
            # A single target is always a full exit on the broker. Never race it with partial close.
            final_tp = order.tp2 or order.tp1
            if reached(final_tp):
                result = await self.broker.close_position(pos.ticket, pos.volume)
                await self.record_management(order.signal_id, pos, "CLOSE", result, reason="FINAL_TP", price=str(final_tp))
                await self.notify(f"{'✅' if result.success else '❌'} TP cuối #{pos.ticket}: {escape(result.comment)}")
                if result.success:
                    await self.cancel_dual_entry_pending_orders_on_close(pos.ticket, reason="Đạt TP cuối")
                continue
            if not self.risk_config.tp1_enabled or order.tp2 is None or not reached(order.tp1):
                continue
            if pos.ticket in completed or pos.ticket in self._tp1_completed_tickets:
                continue
            already_partial = (pos.ticket in previous_partial or pos.ticket in self._tp1_processed_tickets
                               or (order.volume is not None and pos.volume < order.volume))
            can_finish = True
            if not already_partial:
                result, volume = await self.partial_one(pos, self.risk_config.default_partial_close_percent)
                if result.success:
                    self._tp1_processed_tickets.add(pos.ticket)
                    await self.record_management(order.signal_id, pos, "PARTIAL_CLOSE", result,
                                                 reason="TP1_PARTIAL", volume=str(volume))
                    await self.notify(f"✅ TP1 #{pos.ticket}: đã chốt {volume} lot ({self.risk_config.default_partial_close_percent}% yêu cầu)")
                elif not result.raw.get("not_divisible"):
                    await self.record_management(order.signal_id, pos, "PARTIAL_CLOSE", result, reason="TP1_PARTIAL")
                    await self.notify(f"❌ TP1 #{pos.ticket}: {escape(result.comment)}")
                    continue  # No BE or completion marker after a failed broker close.
                elif self.risk_config.tp1_min_volume_action == "skip":
                    await self.record_management(order.signal_id, pos, "TP1_COMPLETE", OrderResult(True), reason="TP1_COMPLETE", skipped="MIN_VOLUME")
                    self._tp1_completed_tickets.add(pos.ticket)
                    continue
                else:
                    await self.notify(f"ℹ️ TP1 #{pos.ticket}: không đủ lot để chốt một phần; giữ nguyên khối lượng.")
            if self.risk_config.auto_breakeven_on_partial:
                result = await self.apply_stop(pos, self.breakeven_target(pos, positions, groups))
                await self.record_management(order.signal_id, pos, "MOVE_SL", result, reason="TP1_BREAKEVEN", sl=str(result.price))
                can_finish = result.success
                if not result.raw.get("unchanged"):
                    await self.notify(f"{'✅' if result.success else '❌'} TP1 #{pos.ticket} · Dời SL: {escape(result.comment)}")
            if can_finish:
                await self.record_management(order.signal_id, pos, "TP1_COMPLETE", OrderResult(True), reason="TP1_COMPLETE")
                self._tp1_completed_tickets.add(pos.ticket)

    @staticmethod
    def entry_role(position, order=None):
        roles = {"bot-min-entry": "MIN_ENTRY", "bot-override-min": "MIN_ENTRY",
                 "bot-max-entry": "MAX_ENTRY", "bot-override-max": "MAX_ENTRY"}
        stored = roles.get(getattr(order, "comment", None))
        live = roles.get(position.comment)
        if stored and live and stored != live:
            return None  # Conflicting provenance must not select the wrong branch.
        return stored or live

    def management_selection(self, action, positions, orders, groups, reply_id, raw_text):
        selected = [p for p in positions if action.side is None or p.position_type == action.side.value]
        if action.target != "MIN_ENTRY":
            return selected
        selected = [p for p in selected if self.entry_role(p, orders.get(p.ticket)) == "MIN_ENTRY"]
        if not selected:
            raise ValueError("Không tìm thấy Min Entry đang mở có nhãn nhánh xác định; không tác động Max Entry hoặc lệnh khác.")
        scopes = {(groups.get(p.ticket) or ("ticket", p.ticket), p.symbol, p.position_type) for p in selected}
        if len(scopes) > 1 and reply_id is None and not re.search(r"(?i)tất cả|toàn bộ|all", raw_text):
            raise ValueError("Có nhiều kèo có Min Entry phù hợp; hãy reply đúng kèo hoặc chỉ rõ 'tất cả'.")
        return selected

    async def unique_bad_entry_scope(self, signal, positions):
        """A message without symbol/reply can target one proven Min branch, never guess among setups."""
        orders = await self.position_orders(positions)
        groups = await self.position_groups(positions, orders)
        action = next(a for a in signal.management_actions if a.target == "MIN_ENTRY")
        targets = self.management_selection(action, positions, orders, groups, None, signal.raw_message)
        tickets = {p.ticket for p in targets}
        scopes = {(groups[p.ticket], p.symbol, p.position_type) for p in targets if groups.get(p.ticket) is not None}
        return [p for p in positions if p.ticket in tickets
                or (groups.get(p.ticket), p.symbol, p.position_type) in scopes]

    async def process_management_signal(self, signal, signal_id, reply_id, link):
        positions = await self.broker.get_positions()
        pending = []
        reply_tag = f"Signal #{reply_id}" if reply_id is not None else signal.symbol or "toàn tài khoản"
        if reply_id is not None:
            tickets = await self.get_tickets_for_reply_message(reply_id)
            if not tickets:
                reason = f"Không tìm thấy kèo của tin reply #{reply_id}; chưa thay đổi vị thế nào."
                await self.set_signal_status(signal_id, "REJECTED", reason)
                await self.notify(reason)
                return
            positions = [p for p in positions if p.ticket in tickets]
            pending = [o for o in await self.broker.get_pending_orders() if o.ticket in tickets]
        elif signal.symbol:
            positions = [p for p in positions if p.symbol.upper().startswith(signal.symbol.upper())]
        elif (not re.search(r"(?i)tất cả|toàn bộ|all", signal.raw_message)
              and any(a.target == "MIN_ENTRY" for a in signal.management_actions)):
            try:
                positions = await self.unique_bad_entry_scope(signal, positions)
            except ValueError as exc:
                await self.set_signal_status(signal_id, "REJECTED", str(exc))
                await self.notify(f"⛔ {escape(str(exc))}")
                return
        elif not re.search(r"(?i)tất cả|toàn bộ|all", signal.raw_message):
            await self.set_signal_status(signal_id, "REJECTED", "Thiếu symbol hoặc reply xác định kèo")
            await self.notify("⛔ Cần symbol, reply đúng kèo hoặc chỉ rõ 'tất cả'.")
            return
        if not positions and not pending:
            await self.set_signal_status(signal_id, "SKIPPED", "Các vị thế đã được đóng trước đó")
            await self.notify(f"ℹ️ {reply_tag}: các lệnh đã được đóng trước đó hoặc không còn vị thế phù hợp.")
            return
        orders = await self.position_orders(positions)
        groups = await self.position_groups(positions, orders)
        # A reply itself is an authoritative group for positions with no DB link.
        if reply_id is not None:
            groups = {p.ticket: (groups.get(p.ticket) or ("reply", reply_id)) for p in positions}
        try:
            selections = [self.management_selection(a, positions, orders, groups, reply_id, signal.raw_message)
                          for a in signal.management_actions]
        except ValueError as exc:
            await self.set_signal_status(signal_id, "REJECTED", str(exc))
            await self.notify(f"⛔ {escape(str(exc))}")
            return
        partial_history, _ = await self.management_history(positions)
        outcomes = []
        has_stop = any(a.action_type in {ManagementType.MOVE_SL, ManagementType.BREAKEVEN} for a in signal.management_actions)
        for action, selected in zip(signal.management_actions, selections):
            if action.action_type == ManagementType.CLOSE:
                for order in pending:
                    if action.side is None or order.order_type.startswith(action.side.value):
                        result = await self.broker.cancel_order(order.ticket)
                        outcomes.append(result.success)
                pending = [o for o in pending if action.side is not None and not o.order_type.startswith(action.side.value)]
            for pos in selected:
                if action.action_type in {ManagementType.CLOSE, ManagementType.PARTIAL_CLOSE}:
                    partial = action.action_type == ManagementType.PARTIAL_CLOSE
                    percentage = self.partial_percentage(action)
                    db_order = orders.get(pos.ticket)
                    already = pos.ticket in partial_history or pos.ticket in self._tp1_processed_tickets
                    already |= bool(db_order and db_order.volume is not None and pos.volume < db_order.volume)
                    if partial and already and not self.risk_config.allow_multiple_partial_closes:
                        await self.notify(f"ℹ️ {reply_tag} · Bỏ qua chốt bớt #{pos.ticket}: Vị thế đã chốt 1 phần trước đó.")
                        continue
                    if partial:
                        result, volume = await self.partial_one(pos, percentage)
                    else:
                        volume = pos.volume
                        result = await self.broker.close_position(pos.ticket, volume)
                    outcomes.append(result.success)
                    await self.record_management(signal_id, pos, action.action_type.value, result, volume=str(volume), target=action.target)
                    label = f"Chốt {percentage.normalize():f}%" if partial else "Đóng lệnh"
                    if action.target == "MIN_ENTRY":
                        label += " Min Entry (entry xấu)"
                    await self.notify(self.format_notification(f"{'✅' if result.success else '❌'} Position Management: {label}\n{reply_tag} · #{pos.ticket} · {escape(result.comment)}", link))
                    if result.success and partial:
                        self._tp1_processed_tickets.add(pos.ticket)
                        partial_history.add(pos.ticket)
                        if self.risk_config.auto_breakeven_on_partial and not has_stop:
                            be = await self.apply_stop(pos, self.breakeven_target(pos, positions, groups))
                            outcomes.append(be.success)
                            await self.record_management(signal_id, pos, "AUTO_BREAKEVEN", be, sl=str(be.price))
                            await self.notify(f"{'✅' if be.success else '❌'} Auto Breakeven on Partial · #{pos.ticket}: {escape(be.comment)}")
                    elif result.success and not partial:
                        await self.cancel_dual_entry_pending_orders_on_close(pos.ticket, reason="Tín hiệu đóng lệnh")
                elif action.action_type in {ManagementType.MOVE_SL, ManagementType.BREAKEVEN}:
                    price = action.price if action.price is not None else self.breakeven_target(pos, positions, groups)
                    result = await self.apply_stop(pos, price, protect=action.price is None)
                    outcomes.append(result.success)
                    await self.record_management(signal_id, pos, action.action_type.value, result, sl=str(result.price))
                    await self.notify(f"{'✅' if result.success else '❌'} {reply_tag} · SL #{pos.ticket}: {escape(result.comment)}")
        status = "EXECUTED" if outcomes and all(outcomes) else "PARTIAL" if any(outcomes) else "ERROR" if outcomes else "SKIPPED"
        await self.set_signal_status(signal_id, status)
