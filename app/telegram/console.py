"""Telegram views and expiring, single-use drafts for user decisions."""
from __future__ import annotations

import secrets
import time
from html import escape

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.constants import ParseMode

from app.telegram.presentation import heading, section, field, number, money, position_card, pending_card, join_cards, config_value, DIVIDER
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
        cards = [heading("⚙️ Xem trước thay đổi", "Chưa lưu thay đổi")]
        for key in updates:
            cards.append(section(CONFIG_FIELDS.get(key, key),
                field("Hiện tại", config_value(getattr(cfg, key))),
                field("Sau khi lưu", config_value(getattr(candidate, key)))))
        lines = [join_cards(cards)]
        token = self.new_draft("config", {"updates": updates, "before": cfg.model_dump(mode="json")})
        return "\n".join(lines), self.draft_keyboard(token)

    async def display_currency(self):
        try:
            account = await self.service.broker.get_account_info()
            return account.currency if isinstance(account.currency, str) else ""
        except Exception:
            return ""

    async def monthly_volume_block(self):
        summary = await self.service.get_monthly_volume()
        title = f"KHỐI LƯỢNG THÁNG {summary['month']}"
        if not summary["available"]:
            return section(title, "Chưa đọc được lịch sử MT5; hãy cập nhật lại.")
        total_lots = number(summary["total"], minimum=2)
        lines = [field("Mở lệnh", f"{number(summary['opened'], minimum=2)} lot"),
                 field("Đóng lệnh", f"{number(summary['closed'], minimum=2)} lot"),
                 f"<b>{field('Tổng hai chiều', total_lots + ' lot')}</b>"]
        if summary['unallocated']:
            lines.append(field("Chưa tách mở/đóng (đảo chiều)", f"{number(summary['unallocated'])} lot"))
        lines.append("<i>Toàn tài khoản · Tháng dương lịch, giờ Việt Nam.</i>")
        return section(title, *lines)

    async def status_view(self):
        from app.telegram.signal_listener import signal_listener
        summary = await self.service.get_status_summary()
        client = signal_listener.client
        listening = bool(signal_listener._running and client and client.is_connected())
        currency = summary["currency"]
        pnl = money(summary["today_pnl"], currency, signed=True) if summary["daily_available"] else "Chưa đọc được dữ liệu"
        blocks = [heading("📊 Tổng quan", "Cập nhật " + now_vietnam().strftime('%H:%M:%S · %d/%m/%Y')),
            section("TRẠNG THÁI", field("Mở lệnh mới", "🟢 Cho phép" if summary['bot_enabled'] else "⏸ Tạm dừng"),
                field("MT5", "✅ Kết nối" if summary['connected'] else "❌ Mất kết nối"),
                field("Nhận tín hiệu", "✅ Kết nối" if listening else "❌ Chưa sẵn sàng")),
            section("TÀI KHOẢN", field("Số tài khoản", summary['login'], code=True), field("Máy chủ", summary['server']),
                field("Số dư", money(summary['balance'], currency)), field("Equity", money(summary['equity'], currency)),
                field("Khả dụng", money(summary['free_margin'], currency))),
            section("HÔM NAY · GIỜ VIỆT NAM", field("Lãi/lỗ đã chốt", pnl)),
            await self.monthly_volume_block(),
            section("CẦN THEO DÕI", field("Vị thế đang mở", summary['open_positions_count']),
                field("Lệnh chờ", summary['pending_orders_count']), field("Tín hiệu chờ duyệt", summary['awaiting_approval']))]
        return join_cards(blocks), build_status_keyboard(summary["bot_enabled"])

    async def positions_view(self, page: int = 0):
        positions = sorted(await self.service.broker.get_positions(), key=lambda p: p.ticket)
        page = min(max(page, 0), max(0, (len(positions) - 1) // 5))
        pages = max(1, (len(positions) + 4) // 5)
        currency = await self.display_currency()
        cards = [heading("📋 Vị thế đang mở", f"Trang {page + 1}/{pages} · {len(positions)} vị thế · Toàn tài khoản")]
        cards += [position_card(pos, currency, page * 5 + i + 1)
                  for i, pos in enumerate(positions[page * 5:page * 5 + 5])]
        if not positions:
            cards.append("Không có vị thế đang mở.")
        else:
            cards.append("<i>Nút thao tác ghi số thứ tự và ticket tương ứng với lệnh bên trên.</i>")
        return join_cards(cards), build_positions_keyboard(positions, page, self.service.risk_config.default_partial_close_percent)

    async def inbox_view(self, page: int = 0, status: str = "ALL"):
        labels = {"ALL": "Tất cả", "AWAITING_APPROVAL": "Chờ duyệt", "EXECUTED": "Đã thực thi",
                  "REJECTED": "Bị chặn", "UNKNOWN": "Không hiểu", "ERROR": "Lỗi", "PARTIAL": "Một phần"}
        rows = await self.service.get_signal_inbox(page, status)
        lines = [heading("📥 Hộp thư tín hiệu", f"{labels.get(status, status)} · Trang {page + 1}")]
        buttons = [[Button("Tất cả", callback_data="inbox_ALL_0"), Button("Chờ duyệt", callback_data="inbox_AWAITING_APPROVAL_0"),
                    Button("Bị chặn", callback_data="inbox_REJECTED_0")],
                   [Button("Đã thực thi", callback_data="inbox_EXECUTED_0"), Button("Không hiểu", callback_data="inbox_UNKNOWN_0"),
                    Button("Lỗi", callback_data="inbox_ERROR_0")]]
        for signal, details in rows[:5]:
            lines.append(DIVIDER)
            lines.append(heading(f"Tín hiệu #{signal.id}") + "\n"
                + field("Trạng thái", labels.get(signal.status, signal.status)) + "\n"
                + field("Nhận lúc", format_vietnam_time(signal.received_at)))
            lines.append("\n<b>NỘI DUNG</b>\n" + escape(signal.raw_message[:220]))
            if details and details.get("reason"):
                lines.append("\n<b>LÝ DO XỬ LÝ</b>\n" + escape(str(details["reason"])[:200]))
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
        # Bound rendered items, so a setup with many tickets cannot overflow one Telegram message.
        entries = [(index, group, item, is_pending)
                   for index, group in enumerate(groups)
                   for is_pending, items in [(False, group["positions"]), (True, group["orders"])]
                   for item in items]
        page = min(max(page, 0), max(0, (len(entries) - 1) // 4))
        pages = max(1, (len(entries) + 3) // 4)
        currency = await self.display_currency()
        cards = [heading("🗂 Kèo đang chạy", f"Trang {page + 1}/{pages} · {len(groups)} kèo · Cập nhật {now_vietnam().strftime('%H:%M:%S')}")]
        previous_group = None
        for index, group, item, is_pending in entries[page * 4:page * 4 + 4]:
            if index != previous_group:
                positions, orders = group["positions"], group["orders"]
                pnl = sum(p.profit for p in positions)
                cards.append(heading(group['name']) + "\n\n"
                    + section("TỔNG KÈO", field("Vị thế đang mở", len(positions)), field("Lệnh chờ", len(orders)),
                        field("Lãi/lỗ đang chạy", money(pnl, currency, signed=True))))
                previous_group = index
            cards.append(pending_card(item) if is_pending else position_card(item, currency))
        if not entries:
            cards.append("Không có kèo đang chạy.")
        buttons = [[Button("Quản lý vị thế", callback_data="positions_page_0"), Button("Lệnh chờ", callback_data="view_orders")]]
        nav = []
        if page:
            nav.append(Button("← Trước", callback_data=f"setups_{page - 1}"))
        if len(entries) > (page + 1) * 4:
            nav.append(Button("Sau →", callback_data=f"setups_{page + 1}"))
        if nav:
            buttons.append(nav)
        buttons.append([Button("Tổng quan", callback_data="status_refresh")])
        return join_cards(cards), InlineKeyboardMarkup(buttons)

    async def pending_orders_view(self, page=0):
        from app.telegram.keyboards import build_pending_orders_keyboard
        orders = sorted(await self.service.broker.get_pending_orders(), key=lambda o: o.ticket)
        page = min(max(page, 0), max(0, (len(orders) - 1) // 5))
        pages = max(1, (len(orders) + 4) // 5)
        cards = [heading("⏳ Lệnh chờ", f"Trang {page + 1}/{pages} · {len(orders)} lệnh")]
        cards += [pending_card(order) for order in orders[page * 5:page * 5 + 5]]
        if not orders:
            cards.append("Không có lệnh chờ.")
        return join_cards(cards), build_pending_orders_keyboard(orders, page)

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
        return join_cards([
            heading("🏥 Kết nối hệ thống", now_vietnam().strftime('%H:%M:%S · %d/%m/%Y')),
            section("KẾT NỐI", field("MT5", "✅ Kết nối" if connected else "❌ Mất kết nối"),
                field("Telegram nhận tín hiệu", "✅ Kết nối" if listener else "❌ Chưa sẵn sàng"),
                field("Database", "✅ Kết nối" if database else "❌ Không truy cập được")),
            section("TÁC VỤ", field("Đồng bộ lệnh", "✅ Đang chạy" if sync else "❌ Chưa chạy"),
                field("Tin gần nhất", last))])

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
        if data.startswith("cfg_group_"):
            from app.telegram.keyboards import CONFIG_SECTIONS
            group = data[len("cfg_group_"):]
            if group in CONFIG_SECTIONS:
                text = heading(CONFIG_SECTIONS[group][0], "Chọn tham số để xem trước thay đổi")
                if group == "sl":
                    text += "\n\nHòa vốn dùng entry riêng từng lệnh.\nKhông tính trung bình hai entry."
                elif group == "profit":
                    text += "\n\nMột TP: đóng toàn bộ.\nTP1 + TP2: chốt theo tỷ lệ rồi giữ phần còn lại."
                await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                    reply_markup=build_risk_config_keyboard(cfg, group))
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
                await query.message.reply_text(f"⚙️ {CONFIG_FIELDS[key]}\n\nHiện tại: {config_value(value)}\n\nNhập giá trị mới hoặc gửi ‘hủy’.\nBot sẽ cho xem trước trước khi lưu.")
            return True
        if data == "view_config_risk":
            await query.edit_message_text("⚙️ <b>Cài đặt</b>\nChọn mục cần sửa. Mọi thay đổi đều có bước xem trước và xác nhận.",
                                          parse_mode=ParseMode.HTML, reply_markup=build_risk_config_keyboard(cfg))
            return True
        if data.startswith("positions_page_") or data in {"view_positions", "positions_refresh"}:
            page = int(data.rsplit("_", 1)[1]) if data.startswith("positions_page_") else 0
            text, keyboard = await self.positions_view(page)
        elif data == "view_orders" or data.startswith("orders_page_"):
            page = int(data.rsplit("_", 1)[1]) if data.startswith("orders_page_") else 0
            text, keyboard = await self.pending_orders_view(page)
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
            text = join_cards([heading("⚠️ Duyệt lệnh thủ công", f"Tín hiệu #{signal_id}"),
                *[section(f"{r.order_type} · {r.symbol}", field("Khối lượng", f"{number(r.volume)} lot"),
                    field("Giá đặt", number(r.price, minimum=2), code=True),
                    field("SL", number(r.sl, minimum=2), code=True), field("TP", number(r.tp, minimum=2), code=True))
                  for r in requests.values()],
                section("ĐIỀU KIỆN ĐƯỢC PHÉP VƯỢT", escape(entry.get("allowed_rule") or "Vùng giá / trượt giá"))])
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
            text = join_cards([heading("⚠️ " + names[action], "Chưa thực thi"),
                section("PHẠM VI", f"Phạm vi: {len(tickets)} ticket trên tài khoản hiện tại."),
                section("TICKET ĐƯỢC CHỌN", "\n".join(f"• <code>#{t}</code>" for t in tickets) or "Không có lệnh phù hợp.")])
            keyboard = self.draft_keyboard(token, bool(tickets))
        else:
            return False
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return True
