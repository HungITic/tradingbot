"""Telegram views and expiring, single-use drafts for user decisions."""
from __future__ import annotations

import secrets
import time
from html import escape

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.constants import ParseMode

from app.core.timezone import format_vietnam_time, now_vietnam
from app.telegram.keyboards import build_positions_keyboard, build_risk_config_keyboard, build_status_keyboard

MENU = ReplyKeyboardMarkup(
    [["📊 Tổng quan", "📥 Tín hiệu"], ["🗂 Kèo đang chạy", "📋 Vị thế"],
     ["📈 Hiệu suất", "⚙️ Cài đặt"], ["🏥 Kết nối"]], resize_keyboard=True,
    input_field_placeholder="Chọn chức năng hoặc dán link tín hiệu để xem trước",
)

CONFIG_FIELDS = {
    "enabled": "Cho phép mở lệnh mới", "require_sl": "Bắt buộc có SL",
    "require_tp": "Bắt buộc có TP", "enable_dual_entry": "Vào hai đầu vùng giá",
    "max_slippage_entry": "Dung sai giá", "lot_min_entry": "Lot nhánh thứ nhất",
    "lot_max_entry": "Lot nhánh thứ hai", "max_open_positions": "Số lệnh tối đa (gồm lệnh chờ)",
    "default_lot": "Lot mặc định", "default_partial_close_percent": "Tỷ lệ chốt một phần (%)",
    "auto_breakeven_on_partial": "Dời SL sau khi chốt một phần",
    "generic_partial_uses_default": "Chốt bớt chung dùng tỷ lệ cấu hình",
    "require_explicit_close": "Yêu cầu từ khóa đóng lệnh rõ ràng",
    "breakeven_only_improve": "Giữ SL đang bảo vệ tốt hơn",
    "breakeven_offset_price": "Bù giá hòa vốn (BUY cộng, SELL trừ)",
    "tp1_enabled": "Tự xử lý TP1 khi có TP2",
    "tp1_min_volume_action": "TP1 khi không đủ lot để chia",
    "auto_cancel_pending_on_full_close": "Hủy nhánh chờ sau khi đóng hết vị thế",
    "allow_multiple_partial_closes": "Cho phép chốt một phần nhiều lần",
    "max_daily_loss_percent": "Giới hạn lỗ ngày (%)", "signal_expire_seconds": "Thời hạn tín hiệu (giây)",
}
POLICY_CHOICES = {
    "tp1_min_volume_action": {"breakeven": "Chỉ dời SL nếu bật tự dời", "skip": "Bỏ qua, giữ nguyên"},
}
LEGACY_CONFIG = {
    "toggle_bot_enabled": "enabled", "toggle_require_sl": "require_sl", "toggle_auto_be": "auto_breakeven_on_partial",
    "toggle_dual_entry": "enable_dual_entry", "cycle_max_slippage": "max_slippage_entry",
    "cycle_lot_min_entry": "lot_min_entry", "cycle_lot_max_entry": "lot_max_entry",
    "edit_max_pos": "max_open_positions", "edit_def_lot": "default_lot",
    "toggle_partial_pct": "default_partial_close_percent",
}


class TelegramConsoleMixin:
    def new_draft(self, kind: str, payload: dict) -> str:
        now = time.monotonic()
        self._drafts = {key: value for key, value in self._drafts.items() if value["expires"] > now}
        token = secrets.token_hex(8)
        self._drafts[token] = {"kind": kind, "payload": payload, "expires": now + 300}
        return token

    def draft_keyboard(self, token: str, execute: bool = True):
        buttons = []
        if execute:
            buttons.append([Button("✅ Xác nhận thực thi", callback_data=f"draft_ok_{token}")])
        buttons.append([Button("❌ Bỏ qua", callback_data=f"draft_no_{token}")])
        return InlineKeyboardMarkup(buttons)

    def prepare_config(self, updates: dict) -> tuple[str, InlineKeyboardMarkup]:
        cfg = self.service.risk_config
        candidate = cfg.with_updates(updates)
        lines = ["⚙️ <b>Xem trước thay đổi — chưa lưu</b>"]
        for key in updates:
            lines.append(f"• {escape(CONFIG_FIELDS.get(key, key))}: <b>{escape(str(getattr(cfg, key)))}</b> → <b>{escape(str(getattr(candidate, key)))}</b>")
        token = self.new_draft("config", {"updates": updates, "before": cfg.model_dump(mode="json")})
        return "\n".join(lines), self.draft_keyboard(token)

    async def status_view(self):
        from app.telegram.signal_listener import signal_listener
        summary = await self.service.get_status_summary()
        client = signal_listener.client
        listening = bool(signal_listener._running and client and client.is_connected())
        pnl = str(summary["today_pnl"]) if summary["daily_available"] else "Chưa đọc được dữ liệu"
        text = (f"📊 <b>Tổng quan · {now_vietnam().strftime('%H:%M:%S %d/%m/%Y')}</b>\n"
                f"Mở lệnh mới: {'🟢 Cho phép' if summary['bot_enabled'] else '⏸ Tạm dừng'}\n"
                f"MT5: {'✅ Kết nối' if summary['connected'] else '❌ Mất kết nối'}\n"
                f"Telegram nhận tín hiệu: {'✅ Kết nối' if listening else '❌ Chưa sẵn sàng'}\n"
                f"Tài khoản: <code>{summary['login']}</code> · {escape(summary['server'])}\n\n"
                f"Số dư: {summary['balance']} {escape(summary['currency'])}\n"
                f"Equity: {summary['equity']} · Khả dụng: {summary['free_margin']}\n"
                f"Lãi/lỗ thực hiện hôm nay (giờ VN): {pnl}\n"
                f"{summary['open_positions_count']} vị thế · {summary['pending_orders_count']} lệnh chờ\n"
                f"{summary['awaiting_approval']} tín hiệu chờ duyệt")
        return text, build_status_keyboard(summary["bot_enabled"])

    async def positions_view(self, page: int = 0):
        positions = sorted(await self.service.broker.get_positions(), key=lambda p: p.ticket)
        page = min(max(page, 0), max(0, (len(positions) - 1) // 5))
        lines = [f"📋 <b>Vị thế · Trang {page + 1}</b>",
                 "Phạm vi: toàn tài khoản. Mỗi hàng nút ứng với ticket phía trên."]
        for pos in positions[page * 5:page * 5 + 5]:
            lines.append(f"\n<b>{escape(pos.symbol)} {escape(pos.position_type)}</b> · #{pos.ticket}\n"
                         f"{pos.volume} lot · Lãi/lỗ: {pos.profit:+.2f}\n"
                         f"Vào: {pos.open_price} → Hiện tại: {pos.current_price}\n"
                         f"SL: {pos.sl or '—'} · TP: {pos.tp or '—'}")
        if not positions:
            lines.append("Không có vị thế đang mở.")
        return "\n".join(lines), build_positions_keyboard(positions, page, self.service.risk_config.default_partial_close_percent)

    async def inbox_view(self, page: int = 0, status: str = "ALL"):
        labels = {"ALL": "Tất cả", "AWAITING_APPROVAL": "Chờ duyệt", "EXECUTED": "Đã thực thi",
                  "REJECTED": "Bị chặn", "UNKNOWN": "Không hiểu", "ERROR": "Lỗi", "PARTIAL": "Một phần"}
        rows = await self.service.get_signal_inbox(page, status)
        lines = [f"📥 <b>Tín hiệu · {labels.get(status, status)} · Trang {page + 1}</b>"]
        buttons = [[Button("Tất cả", callback_data="inbox_ALL_0"), Button("Chờ duyệt", callback_data="inbox_AWAITING_APPROVAL_0"),
                    Button("Bị chặn", callback_data="inbox_REJECTED_0")],
                   [Button("Đã thực thi", callback_data="inbox_EXECUTED_0"), Button("Không hiểu", callback_data="inbox_UNKNOWN_0"),
                    Button("Lỗi", callback_data="inbox_ERROR_0")]]
        for signal, details in rows[:5]:
            lines.append(f"\n<b>#{signal.id} · {labels.get(signal.status, signal.status)}</b> · {format_vietnam_time(signal.received_at)}\n"
                         f"{escape(signal.raw_message[:220])}")
            if details and details.get("reason"):
                lines.append("Lý do: " + escape(str(details["reason"])[:200]))
            if signal.id in self.service._pending_overrides:
                buttons.append([Button(f"Xem quyết định #{signal.id}", callback_data=f"review_{signal.id}")])
        if not rows:
            lines.append("Chưa có tín hiệu trong bộ lọc này.")
        nav = []
        if page:
            nav.append(Button("← Trước", callback_data=f"inbox_{status}_{page - 1}"))
        if len(rows) > 5:
            nav.append(Button("Sau →", callback_data=f"inbox_{status}_{page + 1}"))
        if nav:
            buttons.append(nav)
        buttons.append([Button("Tổng quan", callback_data="status_refresh")])
        return "\n".join(lines), InlineKeyboardMarkup(buttons)

    async def setups_view(self, page: int = 0):
        groups = await self.service.get_setup_cards()
        page = min(max(page, 0), max(0, (len(groups) - 1) // 4))
        lines = [f"🗂 <b>Kèo đang chạy · Trang {page + 1}</b>"]
        for group in groups[page * 4:page * 4 + 4]:
            positions, orders = group["positions"], group["orders"]
            lines.append(f"\n<b>{escape(group['name'])}</b>\n"
                         f"{len(positions)} vị thế · {len(orders)} lệnh chờ\n"
                         f"Lãi/lỗ đang chạy: {sum(p.profit for p in positions):+.2f}")
            for pos in positions:
                lines.append(f"#{pos.ticket} {escape(pos.symbol)} {pos.position_type} · {pos.volume} lot · SL {pos.sl or '—'} · TP {pos.tp or '—'}")
            for order in orders:
                lines.append(f"Chờ #{order.ticket} {escape(order.symbol)} {order.order_type} · {order.volume} lot @ {order.price}")
        if not groups:
            lines.append("Không có kèo đang chạy.")
        buttons = [[Button("Quản lý vị thế", callback_data="positions_page_0"), Button("Lệnh chờ", callback_data="view_orders")]]
        nav = []
        if page:
            nav.append(Button("← Trước", callback_data=f"setups_{page - 1}"))
        if len(groups) > (page + 1) * 4:
            nav.append(Button("Sau →", callback_data=f"setups_{page + 1}"))
        if nav:
            buttons.append(nav)
        buttons.append([Button("Tổng quan", callback_data="status_refresh")])
        return "\n".join(lines), InlineKeyboardMarkup(buttons)

    async def health_view(self):
        from app.telegram.signal_listener import signal_listener
        from app.config.database import get_session
        from sqlalchemy import text
        try:
            connected = await self.service.broker.is_connected()
        except Exception:
            connected = False
        try:
            async with get_session() as session:
                await session.execute(text("SELECT 1"))
            database = True
        except Exception:
            database = False
        client = signal_listener.client
        listener = bool(signal_listener._running and client and client.is_connected())
        if listener:
            try:
                listener = bool(await client.is_user_authorized())
            except Exception:
                listener = False
        task = getattr(self.service, "_sync_task", None)
        sync = bool(task and not task.done())
        last = format_vietnam_time(signal_listener.last_message_at, "%H:%M:%S %d/%m/%Y") if signal_listener.last_message_at else "Chưa nhận tin trong phiên này"
        return (f"🏥 <b>Kết nối · {now_vietnam().strftime('%H:%M:%S %d/%m/%Y')}</b>\n"
                f"MT5: {'✅ Kết nối' if connected else '❌ Mất kết nối'}\n"
                f"Telegram nhận tín hiệu: {'✅ Kết nối' if listener else '❌ Chưa sẵn sàng'}\n"
                f"Database: {'✅ Kết nối' if database else '❌ Không truy cập được'}\n"
                f"Đồng bộ lệnh: {'✅ Đang chạy' if sync else '❌ Chưa chạy'}\n"
                f"Tin gần nhất: {last}")

    async def handle_console_callback(self, update, context, data: str) -> bool:
        query = update.callback_query
        cfg = self.service.risk_config
        if data.startswith("draft_"):
            _, action, token = data.split("_", 2)
            draft = self._drafts.pop(token, None)
            if not draft or draft["expires"] <= time.monotonic():
                await query.edit_message_text("⏳ Yêu cầu đã hết hạn hoặc đã xử lý. Hãy mở lại màn hình.")
                return True
            if action != "ok":
                await query.edit_message_text("Đã bỏ qua; chưa thực thi thay đổi.")
                return True
            payload = draft["payload"]
            if draft["kind"] == "config":
                if cfg.model_dump(mode="json") != payload["before"]:
                    await query.edit_message_text("Cấu hình đã thay đổi. Hãy xem trước lại để tránh ghi đè.")
                    return True
                result = await self.service.update_config_keys(payload["updates"], updated_by="TELEGRAM_CONFIRM", expected=payload["before"])
                await query.edit_message_text("✅ Đã lưu cấu hình." if all(result.values()) else "❌ Không lưu được cấu hình; giá trị cũ được giữ lại.",
                                              reply_markup=build_risk_config_keyboard(cfg))
            elif draft["kind"] == "signal":
                await query.edit_message_text("Đang kiểm tra lại tín hiệu trước thực thi…")
                await self.service.process_raw_message(**payload)
            elif draft["kind"] == "breakeven":
                result = await self.service.move_to_breakeven(payload["ticket"], expected_price=payload["price"])
                await query.edit_message_text(
                    f"{'✅' if result.success else '❌'} #{payload['ticket']} · SL: {result.price or '—'}\n{result.comment}",
                    reply_markup=build_status_keyboard(cfg.enabled))
            elif draft["kind"] == "action":
                kind = payload["action"]
                if kind == "partial":
                    results = []
                    for ticket in payload["tickets"]:
                        rows = await self.service.partial_close_positions(ticket=ticket, percentage=payload["percentage"])
                        results += [r for _, r, _ in rows]
                else:
                    async with self.service._execution_lock:
                        if kind == "cancel":
                            results = [await self.service.broker.cancel_order(t) for t in payload["tickets"]]
                        elif kind == "close":
                            results = [await self.service.close_position_by_ticket(t) for t in payload["tickets"]]
                ok = sum(r.success for r in results)
                await query.edit_message_text(f"Đã thực hiện thành công {ok}/{len(results)} yêu cầu.\n"
                                              + "\n".join(f"#{r.ticket}: {escape(r.comment)}" for r in results if not r.success),
                                              parse_mode=ParseMode.HTML, reply_markup=build_status_keyboard(cfg.enabled))
            return True
        if data.startswith("be_"):
            ticket = int(data.split("_")[1])
            try:
                preview = await self.service.breakeven_preview(ticket)
            except Exception as exc:
                await query.edit_message_text(f"Không xem trước được hòa vốn: {exc}")
                return True
            token = self.new_draft("breakeven", {"ticket": ticket, "price": preview["price"]})
            await query.edit_message_text(preview["text"], parse_mode=ParseMode.HTML, reply_markup=self.draft_keyboard(token))
            return True
        if data.startswith("policy_"):
            _, key, value = data.split(":", 2)
            if key in POLICY_CHOICES and value in POLICY_CHOICES[key]:
                text, keyboard = self.prepare_config({key: value})
                await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            return True
        if data == "cfg_breakeven_mode":
            await query.edit_message_text("Hòa vốn luôn dùng entry riêng từng lệnh. Quy tắc lấy giá trung bình đã được bỏ.")
            return True
        if data.startswith("cfg_") or data in LEGACY_CONFIG:
            key = data[4:] if data.startswith("cfg_") else LEGACY_CONFIG[data]
            if key not in CONFIG_FIELDS:
                return True
            value = getattr(cfg, key)
            if key in POLICY_CHOICES:
                buttons = [[Button(label, callback_data=f"policy_:{key}:{choice}")]
                           for choice, label in POLICY_CHOICES[key].items()]
                buttons.append([Button("Quay lại cài đặt", callback_data="view_config_risk")])
                await query.edit_message_text(CONFIG_FIELDS[key] + " · Chọn giá trị để xem trước:", reply_markup=InlineKeyboardMarkup(buttons))
            elif isinstance(value, bool):
                text, keyboard = self.prepare_config({key: not value})
                await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            else:
                context.user_data["config_field"] = {"key": key, "expires": time.monotonic() + 300}
                await query.message.reply_text(f"{CONFIG_FIELDS[key]} · Hiện tại: {value}\nNhập giá trị mới, hoặc gửi 'hủy'. Bot sẽ cho xem trước trước khi lưu.")
            return True
        if data == "view_config_risk":
            await query.edit_message_text("⚙️ <b>Cài đặt</b>\nChọn mục cần sửa. Mọi thay đổi đều có bước xem trước và xác nhận.",
                                          parse_mode=ParseMode.HTML, reply_markup=build_risk_config_keyboard(cfg))
            return True
        if data.startswith("positions_page_") or data in {"view_positions", "positions_refresh"}:
            page = int(data.rsplit("_", 1)[1]) if data.startswith("positions_page_") else 0
            text, keyboard = await self.positions_view(page)
        elif data.startswith("inbox_"):
            status, page = data[6:].rsplit("_", 1)
            if status not in {"ALL", "AWAITING_APPROVAL", "REJECTED", "UNKNOWN", "EXECUTED", "ERROR", "PARTIAL"}:
                return True
            text, keyboard = await self.inbox_view(max(0, int(page)), status)
        elif data.startswith("setups_"):
            text, keyboard = await self.setups_view(int(data.split("_")[1]))
        elif data.startswith("review_"):
            signal_id = int(data.split("_")[1])
            entry = self.service._pending_overrides.get(signal_id)
            if not entry:
                await query.edit_message_text("Yêu cầu duyệt đã hết hiệu lực. Hãy mở lại tín hiệu gốc để xem trước.")
                return True
            from app.telegram.keyboards import build_override_keyboard
            requests = entry["orders"]
            text = "⚠️ <b>Quyết định thủ công</b>\n" + "\n".join(
                f"{r.order_type} {escape(r.symbol)} · {r.volume} lot @ {r.price}" for r in requests.values())
            text += "\nGiới hạn được phép vượt: " + escape(entry.get("allowed_rule") or "vùng giá/trượt giá")
            keyboard = build_override_keyboard(signal_id, len(requests) > 1)
        elif data in {"close_all_confirm", "cancel_all_orders"} or data.startswith(("close_", "cancel_", "partial_")):
            percentage = cfg.default_partial_close_percent
            if data == "close_all_confirm" or data.startswith("partial_all_"):
                tickets = [p.ticket for p in await self.service.broker.get_positions()]
                action = "close" if data == "close_all_confirm" else "partial"
            elif data == "cancel_all_orders":
                tickets = [o.ticket for o in await self.service.broker.get_pending_orders()]
                action = "cancel"
            else:
                parts = data.split("_")
                if len(parts) < 2 or not parts[1].isdigit():
                    return True
                action, tickets = parts[0], [int(parts[1])]
            if action == "partial":
                from decimal import Decimal
                percentage = Decimal(data.rsplit("_", 1)[1])
                if not percentage.is_finite() or not 0 < percentage < 100:
                    return True
            payload = {"action": action, "tickets": tickets, "percentage": percentage}
            token = self.new_draft("action", payload)
            names = {"close": "Đóng vị thế", "cancel": "Hủy lệnh chờ", "partial": f"Chốt {percentage}%"}
            text = f"⚠️ <b>{names[action]}</b>\nPhạm vi: {len(tickets)} ticket trên tài khoản hiện tại.\n" + escape(", ".join(map(str, tickets)))
            keyboard = self.draft_keyboard(token, bool(tickets))
        else:
            return False
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return True
