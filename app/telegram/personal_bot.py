"""
Personal Telegram Bot — Admin Console & Control Plane.
"""

from __future__ import annotations

import dataclasses
import functools
import re
import time
from html import escape
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.telegram.history_periods import history_period
from app.telegram.presentation import heading, section, field, money, number, price, history_card, join_cards
from app.config.settings import settings
from app.telegram.keyboards import (
    build_history_keyboard,
    build_positions_keyboard,
    build_risk_config_keyboard,
    build_stats_keyboard,
    build_status_keyboard,
)
from app.core.timezone import format_vietnam_time, now_vietnam
from app.trading.service import TradingService, trading_service

logger = structlog.get_logger(__name__)


def admin_only(func):
    """Decorator ensuring only the authorized admin chat or user can trigger commands."""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any):
        update: Update | None = None
        for arg in args:
            if isinstance(arg, Update):
                update = arg
                break
        if not update:
            update = kwargs.get("update")

        user_id = update.effective_user.id if (update and update.effective_user) else 0
        chat_id = update.effective_chat.id if (update and update.effective_chat) else 0
        admin_id = settings.telegram.admin_chat_id

        if admin_id and chat_id != admin_id and user_id != admin_id:
            logger.warning("unauthorized_access_attempt", chat_id=chat_id, user_id=user_id, admin_id=admin_id)
            target = update.effective_message if update else None
            if target:
                await target.reply_text(
                    f"⛔ <b>Từ Chối Truy Cập</b>\n"
                    f""
                    f"User ID: <code>{user_id}</code>\n"
                    f"Chat ID: <code>{chat_id}</code>\n"
                    f"Admin ID: <code>{admin_id}</code>",
                    parse_mode=ParseMode.HTML,
                )
            return
        return await func(*args, **kwargs)

    return wrapper


from app.telegram.console import TelegramConsoleMixin, MENU


class PersonalTelegramBot(TelegramConsoleMixin):
    """Manages the Admin Console Telegram Bot lifecycle."""

    def __init__(self, service: TradingService | None = None) -> None:
        self.service = service or trading_service
        self.app: Application | None = None
        self._running = False
        self._drafts: dict[str, dict] = {}

    async def initialize(self) -> None:
        """Build bot application and register all command & callback handlers."""
        if not settings.telegram.bot_token:
            logger.warning("telegram_bot_token_missing")
            return

        self.app = Application.builder().token(settings.telegram.bot_token).build()

        # Connect notification dispatch
        self.service.set_notification_callback(self.send_notification)

        # Register Commands
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("help", self.cmd_help))
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("menu", self.cmd_start))
        self.app.add_handler(CommandHandler("inbox", self.cmd_inbox))
        self.app.add_handler(CommandHandler("setups", self.cmd_setups))
        self.app.add_handler(CommandHandler("positions", self.cmd_positions))
        self.app.add_handler(CommandHandler("balance", self.cmd_balance))

        # History & Win/Loss Monitoring Commands
        self.app.add_handler(CommandHandler("history", self.cmd_history))
        self.app.add_handler(CommandHandler("trades", self.cmd_history))
        self.app.add_handler(CommandHandler("deals", self.cmd_history))
        self.app.add_handler(CommandHandler("stats", self.cmd_stats))
        self.app.add_handler(CommandHandler("summary", self.cmd_stats))
        self.app.add_handler(CommandHandler("winloss", self.cmd_stats))
        self.app.add_handler(CommandHandler("pnl", self.cmd_stats))

        # Position Management Commands
        self.app.add_handler(CommandHandler("close", self.cmd_close))
        self.app.add_handler(CommandHandler("closeall", self.cmd_closeall))
        self.app.add_handler(CommandHandler("partial", self.cmd_partial))
        self.app.add_handler(CommandHandler("partialall", self.cmd_partialall))
        self.app.add_handler(CommandHandler("chotbot", self.cmd_partial))
        self.app.add_handler(CommandHandler("setsl", self.cmd_setsl))
        self.app.add_handler(CommandHandler("settp", self.cmd_settp))
        self.app.add_handler(CommandHandler("breakeven", self.cmd_breakeven))

        # Bot Control Commands
        self.app.add_handler(CommandHandler("pause", self.cmd_pause))
        self.app.add_handler(CommandHandler("resume", self.cmd_resume))
        self.app.add_handler(CommandHandler("health", self.cmd_health))

        # Dynamic Config Commands
        self.app.add_handler(CommandHandler("config", self.cmd_config))

        # Vietnamese Keyword & Management Rules Commands
        self.app.add_handler(CommandHandler("rules", self.cmd_rules))
        self.app.add_handler(CommandHandler("keywords", self.cmd_rules))
        self.app.add_handler(CommandHandler("rule", self.cmd_rule))

        # Signal Replay / Message Link Commands
        self.app.add_handler(CommandHandler("replay", self.cmd_replay))
        self.app.add_handler(CommandHandler("signal", self.cmd_replay))
        self.app.add_handler(CommandHandler("reload_signal", self.cmd_replay))

        # Handle Raw Telegram Links Sent Directly to Bot Chat
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_message))

        # Emergency Commands
        self.app.add_handler(CommandHandler("kill", self.cmd_kill))
        self.app.add_handler(CommandHandler("disable", self.cmd_kill))

        # Inline Button Callbacks
        self.app.add_handler(CallbackQueryHandler(self.handle_callback))

        # Register Global Error Handler
        self.app.add_error_handler(self.error_handler)

        await self.app.initialize()

    async def start(self) -> None:
        """Start receiving updates via polling."""
        if not self.app:
            await self.initialize()
        if self.app:
            self._running = True
            await self.app.start()
            if self.app.updater:
                await self.app.updater.start_polling(drop_pending_updates=False)
            logger.info("personal_telegram_bot_started")

    async def stop(self) -> None:
        """Gracefully stop the Telegram bot."""
        if not self._running or not self.app:
            return
        self._running = False
        logger.info("personal_telegram_bot_stopping")
        try:
            if self.app.updater and self.app.updater.running:
                await self.app.updater.stop()
            if self.app.running:
                await self.app.stop()
            await self.app.shutdown()
        except Exception as e:
            logger.warning("personal_telegram_bot_stop_error", error=str(e))
        logger.info("personal_telegram_bot_stopped")

    async def send_notification(self, text: str, reply_markup=None) -> None:
        """Push broadcast notification to Admin."""
        if self.app and settings.telegram.admin_chat_id:
            try:
                await self.app.bot.send_message(
                    chat_id=settings.telegram.admin_chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
            except Exception as e:
                logger.error("telegram_send_failed", error=str(e))

    # -------------------------------------------------------------
    # Command Handlers
    # -------------------------------------------------------------

    @admin_only
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(
            "🤖 Trading Bot — Bảng điều khiển\n\n"
            "Chọn chức năng ở menu bên dưới. Dán link Telegram để xem trước tín hiệu.\n"
            "Cấu hình, đóng lệnh, chốt một phần và hủy lệnh có bước xác nhận.\n"
            "Gửi /help để xem lệnh nâng cao; /kill để dừng khẩn cấp toàn tài khoản.", reply_markup=MENU)

    @admin_only
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = (
            "📖 <b>Danh Mục Lệnh</b>\n"
            "\n"
            "<b>📊 Giao dịch &amp; Vị thế:</b>\n"
            "/status — Tổng quan hệ thống\n"
            "/inbox — Hộp thư tín hiệu và lý do xử lý\n"
            "/setups — Các kèo đang chạy\n"
            "/positions — Vị thế đang mở\n"
            "/history [ngày] — Lịch sử lệnh đã đóng\n"
            "/stats [ngày] — Thống kê Win/Loss\n"
            "/balance — Số dư &amp; Equity\n"
            "/close &lt;ticket&gt; — Đóng lệnh cụ thể\n"
            "/closeall — Đóng tất cả lệnh\n"
            "/partial &lt;%&gt; — Chốt bớt tất cả lệnh (VD: /partial 50)\n"
            "/partial &lt;%&gt; &lt;symbol&gt; — Chốt bớt theo cặp (VD: /partial 50 XAU)\n"
            "/partial &lt;ticket&gt; &lt;%&gt; — Chốt bớt 1 lệnh (VD: /partial 123456 50)\n"
            "/partialall &lt;%&gt; [symbol] — Đóng một phần các lệnh\n"
            "/setsl &lt;ticket&gt; &lt;giá&gt; — Đặt Stop Loss\n"
            "/settp &lt;ticket&gt; &lt;giá&gt; — Đặt Take Profit\n"
            "/breakeven &lt;ticket&gt; — Dời SL về Entry\n\n"
            "<b>🔄 Nạp lại tín hiệu:</b>\n"
            "/replay &lt;link&gt; — Nạp tín hiệu từ link Telegram\n"
            "  <i>Hoặc dán trực tiếp link vào chat</i>\n\n"
            "<b>📝 Từ khóa quản lý lệnh:</b>\n"
            "/rules — Xem danh sách từ khóa\n"
            "/rule add &lt;từ khóa&gt; &lt;action&gt; [params] — Thêm\n"
            "/rule del &lt;từ khóa&gt; — Xóa\n"
            "/rules reload — Nạp lại từ DB\n\n"
            "<b>⚙️ Cấu hình:</b>\n"
            "/config — Xem tất cả cấu hình\n"
            "/config risk — Nút chỉnh nhanh\n"
            "/config set &lt;key&gt; &lt;value&gt; — Cập nhật tham số\n\n"
            "<b>🛑 Điều khiển Bot:</b>\n"
            "/pause — Tạm dừng giao dịch\n"
            "/resume — Tiếp tục giao dịch\n"
            "/health — Kiểm tra kết nối\n"
            "/kill — Dừng khẩn cấp\n"
            ""
        )
        if update.effective_message:
            await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text, keyboard = await self.status_view()
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    @admin_only
    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text, keyboard = await self.positions_view()
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    @admin_only
    async def cmd_inbox(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text, keyboard = await self.inbox_view()
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    @admin_only
    async def cmd_setups(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text, keyboard = await self.setups_view()
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    @admin_only
    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        acc = await self.service.broker.get_account_info()
        msg = join_cards([heading("💰 Tài khoản", f"#{acc.login} · {acc.server}"),
            section("TÀI SẢN", field("Số dư", money(acc.balance, acc.currency)),
                field("Equity", money(acc.equity, acc.currency))),
            section("KÝ QUỸ", field("Đang sử dụng", money(acc.margin, acc.currency)),
                field("Khả dụng", money(acc.free_margin, acc.currency)), field("Đòn bẩy", f"1:{acc.leverage}"))])
        if update.effective_message:
            await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)

    async def _format_history_message(self, days: int | str = "today", page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
        start, end, label = history_period(days)
        positions = await self.service.get_closed_positions(limit=None, start_time=start, end_time=end)
        pages = max(1, (len(positions) + 2) // 3)
        page = min(max(page, 0), pages - 1)
        visible = positions[page * 3:page * 3 + 3]
        cards = [heading("📜 Lịch sử lệnh", f"{label} · Trang {page + 1}/{pages} · {len(positions)} vị thế · Mới nhất trước · Giờ VN")]
        cards.extend(history_card(pos) for pos in visible)
        if not positions:
            cards.append("Chưa có lệnh chốt trong kỳ này.")
        keyboard = build_history_keyboard(days, page, pages)
        details = [[InlineKeyboardButton(f"#{pos.ticket} · Chi tiết", callback_data=f"history_detail_{days}_{page}_{pos.ticket}_0")
                    for pos in visible]] if visible else []
        return join_cards(cards), InlineKeyboardMarkup(details + list(keyboard.inline_keyboard))

    async def _format_history_detail(self, days: int | str, page: int, ticket: int, deal_page: int = 0):
        start, end, label = history_period(days)
        positions = await self.service.get_closed_positions(limit=None, start_time=start, end_time=end)
        pos = next((pos for pos in positions if pos.ticket == ticket), None)
        back = [InlineKeyboardButton("← Danh sách lệnh", callback_data=f"history_{days}_{page}")]
        if pos is None:
            return "Lệnh không còn trong kỳ đang xem. Hãy cập nhật danh sách.", InlineKeyboardMarkup([back])
        from app.core.timezone import to_vietnam_datetime
        deals = sorted(pos.partial_deals, key=lambda d: (to_vietnam_datetime(d.time), d.ticket), reverse=True)
        pages = max(1, (len(deals) + 4) // 5)
        deal_page = min(max(deal_page, 0), pages - 1)
        blocks = [heading("📜 Chi tiết lệnh", label + " · Giờ Việt Nam"), history_card(pos),
            section("Lãi/lỗ đã ghi nhận", field("Lãi/lỗ giá", money(pos.profit, pos.currency, signed=True)),
                field("Swap", money(pos.swap, pos.currency, signed=True)),
                field("Commission", money(pos.commission, pos.currency, signed=True))),
            section(f"Các lần chốt · {deal_page + 1}/{pages}", *[
                f"{format_vietnam_time(d.time)} · {number(d.volume)} lot @ {price(d.price)}\n"
                f"<code>#{d.ticket}</code> · {escape(money(d.profit, pos.currency, signed=True))}"
                for d in deals[deal_page * 5:deal_page * 5 + 5]]) if deals else "Chưa có chi tiết từng lần chốt."]
        nav = []
        if deal_page:
            nav.append(InlineKeyboardButton("← Mới hơn", callback_data=f"history_detail_{days}_{page}_{ticket}_{deal_page - 1}"))
        if deal_page + 1 < pages:
            nav.append(InlineKeyboardButton("Cũ hơn →", callback_data=f"history_detail_{days}_{page}_{ticket}_{deal_page + 1}"))
        return join_cards(blocks), InlineKeyboardMarkup(([nav] if nav else []) + [back])

    async def _format_stats_message(self, days: int = 7) -> tuple[str, InlineKeyboardMarkup]:
        stats = await self.service.get_performance_summary(days=days)
        net_pnl = stats["net_pnl"]
        pnl_icon = "🟢" if net_pnl > Decimal("0") else ("🔴" if net_pnl < Decimal("0") else "⚪")

        pf_val = stats["profit_factor"]
        pf_str = f"{pf_val:.2f}" if pf_val < 900 else "∞ (Chưa có lệnh thua)"

        currency = await self.display_currency()
        msg = join_cards([
            heading("📈 Báo cáo hiệu suất", f"Kỳ thống kê: {days} ngày"),
            section("KẾT QUẢ", f"{pnl_icon} <b>Lãi ròng: {escape(money(net_pnl, currency, signed=True))}</b>"),
            section("Giao dịch", f"{stats['total_trades']} lệnh · Thắng {stats['win_trades']} · Thua {stats['loss_trades']} · Hòa {stats['breakeven_trades']}",
                field("Tỷ lệ thắng", f"{stats['win_rate']}%") + " · " + field("Profit Factor", pf_str)),
            section("LÃI VÀ LỖ", field("Tổng lãi", money(stats['total_profit'], currency, signed=True)),
                field("Tổng lỗ", money(stats['total_loss'], currency, signed=True))),
            await self.monthly_volume_block(),
            section("LỆNH NỔI BẬT", field("Lãi lớn nhất", money(stats['best_trade'], currency, signed=True)),
                field("Lỗ lớn nhất", money(stats['worst_trade'], currency, signed=True)))])
        keyboard = build_stats_keyboard(days)
        return msg, keyboard

    @admin_only
    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        days = context.args[0] if context.args else "today"
        try:
            history_period(days)
        except (ValueError, TypeError):
            days = "today"
        msg, keyboard = await self._format_history_message(days)
        if update.effective_message:
            await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    @admin_only
    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        days = 7
        if context.args:
            try:
                days = max(1, int(context.args[0]))
            except ValueError:
                pass
        msg, keyboard = await self._format_stats_message(days)
        if update.effective_message:
            await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)


    @admin_only
    async def cmd_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.message.reply_text(
                "⚠️ <b>Cú pháp:</b> <code>/close &lt;ticket&gt;</code>\n"
                "<i>Ví dụ:</i> <code>/close 12345678</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        try:
            ticket = int(context.args[0])
            res = await self.service.close_position_by_ticket(ticket)
            if res.success:
                await update.message.reply_text(
                    f"✅ <b>Đã Đóng Vị Thế</b>\n"
                    f""
                    f"• Ticket: <code>{ticket}</code>\n"
                    f"• Kết quả: Thành công",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await update.message.reply_text(
                    f"❌ <b>Đóng Vị Thế Thất Bại</b>\n"
                    f""
                    f"• Ticket: <code>{ticket}</code>\n"
                    f"• Lý do: {res.comment}",
                    parse_mode=ParseMode.HTML,
                )
        except ValueError:
            await update.message.reply_text("❌ Ticket phải là số nguyên.")

    @admin_only
    async def cmd_closeall(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        results = await self.service.close_all_positions()
        if not results:
            await update.message.reply_text("📂 <b>Không có vị thế nào đang mở.</b>", parse_mode=ParseMode.HTML)
            return

        successes = sum(1 for r in results if r.success)
        await update.message.reply_text(
            f"🚨 <b>Đã Đóng Toàn Bộ Vị Thế</b>\n"
            f""
            f"• Đã đóng thành công: <b>{successes}/{len(results)}</b> vị thế",
            parse_mode=ParseMode.HTML,
        )

    @admin_only
    async def cmd_partial(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        if not args:
            await update.message.reply_text(
                "⚠️ <b>Cú pháp lệnh chốt bớt (Partial Close):</b>\n"
                ""
                "• <b>Chốt tất cả vị thế:</b>\n"
                "  <code>/partial &lt;%&gt;</code>\n"
                "  <i>Ví dụ:</i> <code>/partial 50</code> (chốt 50% tất cả vị thế đang mở)\n\n"
                "• <b>Chốt tất cả vị thế của 1 cặp:</b>\n"
                "  <code>/partial &lt;%&gt; &lt;symbol&gt;</code>\n"
                "  <i>Ví dụ:</i> <code>/partial 50 XAU</code>\n\n"
                "• <b>Chốt 1 lệnh cụ thể:</b>\n"
                "  <code>/partial &lt;ticket&gt; &lt;%&gt;</code>\n"
                "  <i>Ví dụ:</i> <code>/partial 342003626 50</code>\n"
                ""
                "<i>Lệnh tương đương:</i> <code>/partialall &lt;%&gt; [symbol]</code> hoặc <code>/chotbot &lt;%&gt;</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        # Case 1: 1 argument -> /partial <percent> (e.g. /partial 50 or /partial 50%)
        if len(args) == 1:
            raw_arg = args[0].replace("%", "").strip()
            try:
                percent = Decimal(raw_arg)
            except Exception:
                await update.message.reply_text(
                    "❌ <b>Phần trăm không hợp lệ.</b>\n"
                    "Cú pháp chốt tất cả: <code>/partial 50</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            results = await self.service.partial_close_positions(percentage=percent)
            if not results:
                await update.message.reply_text("📂 <b>Không có vị thế nào đang mở để chốt bớt.</b>", parse_mode=ParseMode.HTML)
                return

            lines = [f"✂️ <b>Chốt Bớt {percent}% Cho Tất Cả Vị Thế</b>"]
            for pos, res, vol in results:
                icon = "✅" if res.success else "❌"
                rem = max(Decimal("0.00"), pos.volume - vol) if res.success else pos.volume
                status_text = f"Đã đóng: <b>{vol} lot</b> (còn {rem} lot) · {escape(res.comment)}" if res.success else f"Lỗi: {escape(res.comment)}"
                lines.append(f"{icon} Ticket <code>{pos.ticket}</code> ({pos.symbol}): {status_text}")

            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
            return

        # Case 2: 2 arguments
        arg1, arg2 = args[0].strip(), args[1].strip()

        # Check if arg1 is percent and arg2 is symbol (e.g., /partial 50 XAU)
        try:
            percent_first = Decimal(arg1.replace("%", ""))
            if not arg2.isdigit():
                symbol_filter = arg2
                results = await self.service.partial_close_positions(percentage=percent_first, symbol=symbol_filter)
                if not results:
                    await update.message.reply_text(
                        f"📂 <b>Không có vị thế nào cho symbol '{symbol_filter}' để chốt bớt.</b>",
                        parse_mode=ParseMode.HTML,
                    )
                    return

                lines = [f"✂️ <b>Chốt Bớt {percent_first}% Vị Thế ({symbol_filter.upper()})</b>"]
                for pos, res, vol in results:
                    icon = "✅" if res.success else "❌"
                    rem = max(Decimal("0.00"), pos.volume - vol) if res.success else pos.volume
                    status_text = f"Đã đóng: <b>{vol} lot</b> (còn {rem} lot) · {escape(res.comment)}" if res.success else f"Lỗi: {escape(res.comment)}"
                    lines.append(f"{icon} Ticket <code>{pos.ticket}</code> ({pos.symbol}): {status_text}")

                await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
                return
        except Exception:
            pass

        # Otherwise: /partial <ticket> <percent> (e.g. /partial 342003626 50)
        try:
            ticket = int(arg1)
            percent = Decimal(arg2.replace("%", ""))
            results = await self.service.partial_close_positions(ticket=ticket, percentage=percent)
            if not results:
                await update.message.reply_text(f"Không tìm thấy ticket {ticket}.")
                return
            target, res, vol = results[0]
            if res.success:
                rem_vol = target.volume - vol
                await update.message.reply_text(
                    f"✅ <b>Chốt Lời Một Phần Thành Công</b>\n"
                    f""
                    f"• Ticket: <code>{ticket}</code> ({target.symbol})\n"
                    f"• Đã đóng: <b>{vol} lot</b> ({percent}%)\n"
                    f"• Còn lại: <b>{rem_vol} lot</b>\n"
                    f"• Chi tiết: {escape(res.comment)}",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await update.message.reply_text(
                    f"❌ <b>Chốt Lời Thất Bại</b>\n"
                    f""
                    f"• Ticket: <code>{ticket}</code>\n"
                    f"• Lý do: {res.comment}",
                    parse_mode=ParseMode.HTML,
                )
        except Exception as e:
            await update.message.reply_text(f"❌ Lỗi: <code>{e}</code>", parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_partialall(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Alias for closing partial volume across positions (defaults to 50%)."""
        args = list(context.args or [])
        if not args:
            args = ["50"]
        context.args = args
        await self.cmd_partial(update, context)

    @admin_only
    async def cmd_setsl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text(
                "⚠️ <b>Cú pháp:</b> <code>/setsl &lt;ticket&gt; &lt;giá&gt;</code>\n"
                "<i>Ví dụ:</i> <code>/setsl 123456 2650.5</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            ticket = int(args[0])
            price = Decimal(args[1])
            res = await self.service.modify_sl_tp(ticket, sl=price)
            if res.success:
                await update.message.reply_text(
                    f"✅ <b>Đã Cập Nhật Stop Loss</b>\n"
                    f""
                    f"• Ticket: <code>{ticket}</code>\n"
                    f"• SL mới: <code>{price}</code>",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await update.message.reply_text(
                    f"❌ <b>Cập Nhật SL Thất Bại</b>\n"
                    f""
                    f"• Ticket: <code>{ticket}</code>\n"
                    f"• Lý do: {res.comment}",
                    parse_mode=ParseMode.HTML,
                )
        except Exception as e:
            await update.message.reply_text(f"❌ Lỗi: <code>{e}</code>", parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_settp(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text(
                "⚠️ <b>Cú pháp:</b> <code>/settp &lt;ticket&gt; &lt;giá&gt;</code>\n"
                "<i>Ví dụ:</i> <code>/settp 123456 2680.0</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            ticket = int(args[0])
            price = Decimal(args[1])
            res = await self.service.modify_sl_tp(ticket, tp=price)
            if res.success:
                await update.message.reply_text(
                    f"✅ <b>Đã Cập Nhật Take Profit</b>\n"
                    f""
                    f"• Ticket: <code>{ticket}</code>\n"
                    f"• TP mới: <code>{price}</code>",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await update.message.reply_text(
                    f"❌ <b>Cập Nhật TP Thất Bại</b>\n"
                    f""
                    f"• Ticket: <code>{ticket}</code>\n"
                    f"• Lý do: {res.comment}",
                    parse_mode=ParseMode.HTML,
                )
        except Exception as e:
            await update.message.reply_text(f"❌ Lỗi: <code>{e}</code>", parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_breakeven(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.message.reply_text(
                "⚠️ <b>Cú pháp:</b> <code>/breakeven &lt;ticket&gt;</code>\n"
                "<i>Ví dụ:</i> <code>/breakeven 123456</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            ticket = int(context.args[0])
            res = await self.service.move_to_breakeven(ticket)
            await update.message.reply_text(
                f"{'✅' if res.success else '❌'} <b>Hòa vốn · #{ticket}</b>\n"
                f"SL kết quả: <code>{res.price or '—'}</code>\n{escape(res.comment)}",
                parse_mode=ParseMode.HTML)
        except Exception as exc:
            await update.message.reply_text(f"Không xử lý được hòa vốn: {exc}")

    @admin_only
    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.service.update_config_key("enabled", False, updated_by="PAUSE")
        now_vn = now_vietnam().strftime("%H:%M:%S %d/%m/%Y")
        await update.message.reply_text(
            f"⏸️ <b>Đã Tạm Dừng Bot</b>\n"
            f""
            f"• Trạng thái: <b>TẠM DỪNG (Paused)</b>\n"
            f"• Bot sẽ không vào lệnh mới từ tín hiệu.\n"
            f"⏱ {now_vn}",
            parse_mode=ParseMode.HTML,
        )

    @admin_only
    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.service.update_config_key("enabled", True, updated_by="RESUME")
        now_vn = now_vietnam().strftime("%H:%M:%S %d/%m/%Y")
        await update.message.reply_text(
            f"▶️ <b>Đã Kích Hoạt Lại Bot</b>\n"
            f""
            f"• Trạng thái: <b>HOẠT ĐỘNG (Active)</b>\n"
            f"• Bot sẵn sàng nhận và xử lý tín hiệu mới.\n"
            f"⏱ {now_vn}",
            parse_mode=ParseMode.HTML,
        )

    @admin_only
    async def cmd_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(await self.health_view(), parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_kill(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        report = await self.service.emergency_stop()
        closes, cancels = report["closes"], report["cancellations"]
        self._drafts.clear()
        lines = ["🚨 <b>Đã dừng mở lệnh mới · Phạm vi toàn tài khoản</b>",
                 f"Lưu trạng thái dừng: {'✅ Thành công' if report['saved'] else '❌ Thất bại; cần kiểm tra trước khi khởi động lại'}",
                 f"Đóng vị thế: {sum(r.success for r in closes)}/{len(closes)} thành công",
                 f"Hủy lệnh chờ: {sum(r.success for r in cancels)}/{len(cancels)} thành công",
                 f"Còn lại: {report['remaining_positions']} vị thế · {report['remaining_orders']} lệnh chờ"]
        lines += [f"❌ #{r.ticket}: {escape(r.comment)}" for r in closes + cancels if not r.success]
        lines += [escape(e) for e in report["errors"]]
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    @staticmethod
    def _extract_config_pairs(raw_text: str, args: list[str]) -> list[tuple[str, str]]:
        """
        Extract (key, value) pairs from single-line or multi-line command texts.
        Supports:
          - /config set key val
          - /config set key=val
          - /config key val
          - /config key=val
          - Multiple lines of above (e.g. batch pasting /config set ...)
          - Multiple /config commands on one line
          - Multiple key=val or alternating key val pairs on one line
        """
        pairs: list[tuple[str, str]] = []
        raw_lines = [l.strip() for l in raw_text.splitlines() if l.strip()]

        if len(raw_lines) > 1:
            for line in raw_lines:
                clean = re.sub(r"^/?config\s*", "", line, flags=re.IGNORECASE).strip()
                if clean.lower().startswith("set"):
                    clean = clean[3:].strip()
                if not clean:
                    continue
                if "=" in clean:
                    k, v = clean.split("=", 1)
                    pairs.append((k.strip().lower(), v.strip()))
                elif ":" in clean:
                    k, v = clean.split(":", 1)
                    pairs.append((k.strip().lower(), v.strip()))
                else:
                    parts = clean.split(None, 1)
                    if len(parts) == 2:
                        pairs.append((parts[0].strip().lower(), parts[1].strip()))
            return pairs

        # Single line: check if multiple /config commands appear on the same line
        if len(re.findall(r"/(?:config\b)", raw_text, flags=re.IGNORECASE)) > 1:
            chunks = [c.strip() for c in re.split(r"(?=/(?:config\b))", raw_text) if c.strip()]
            for chunk in chunks:
                clean = re.sub(r"^/?config\s*", "", chunk, flags=re.IGNORECASE).strip()
                if clean.lower().startswith("set"):
                    clean = clean[3:].strip()
                if not clean:
                    continue
                if "=" in clean:
                    k, v = clean.split("=", 1)
                    pairs.append((k.strip().lower(), v.strip()))
                elif ":" in clean:
                    k, v = clean.split(":", 1)
                    pairs.append((k.strip().lower(), v.strip()))
                else:
                    parts = clean.split(None, 1)
                    if len(parts) == 2:
                        pairs.append((parts[0].strip().lower(), parts[1].strip()))
            return pairs

        # Standard args processing for single-line command
        remaining = args[1:] if (args and args[0].lower() == "set") else args
        if not remaining:
            return pairs

        if all("=" in token for token in remaining):
            for token in remaining:
                k, v = token.split("=", 1)
                pairs.append((k.strip().lower(), v.strip()))
            return pairs

        from app.trading.risk.models import RiskConfig
        valid_keys = set(RiskConfig.model_fields.keys())

        if len(remaining) >= 4 and len(remaining) % 2 == 0:
            keys_candidates = [remaining[i].lower() for i in range(0, len(remaining), 2)]
            if all(k in valid_keys for k in keys_candidates):
                for i in range(0, len(remaining), 2):
                    pairs.append((remaining[i].lower(), remaining[i + 1].strip()))
                return pairs

        if len(remaining) == 1 and "=" in remaining[0]:
            k, v = remaining[0].split("=", 1)
            pairs.append((k.strip().lower(), v.strip()))
        elif len(remaining) >= 2:
            k = remaining[0].strip()
            v = " ".join(remaining[1:]).strip()
            pairs.append((k.lower(), v))

        return pairs

    @admin_only
    async def cmd_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        target = update.effective_message or update.message
        if not target:
            return

        try:
            args = list(context.args or [])
            cfg = self.service.risk_config
            user_id = update.effective_user.id if update.effective_user else 0
            raw_text = (target.text or "").strip()
            logger.info("cmd_config_received", args=args, user_id=user_id, raw_text=raw_text)

            if args and args[0].lower() == "reload":
                await self.service.load_risk_config_from_db()
                await target.reply_text("🔄 Đã tải lại toàn bộ cấu hình từ database!", parse_mode=ParseMode.HTML)
                return

            pairs = self._extract_config_pairs(raw_text, args)

            if pairs:
                try:
                    text, keyboard = self.prepare_config({k.lower(): v for k, v in pairs})
                    await target.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
                except (ValueError, TypeError) as exc:
                    await target.reply_text(f"❌ Cấu hình không hợp lệ: {escape(str(exc))}", parse_mode=ParseMode.HTML)
                return

            await target.reply_text(
                "⚙️ <b>Cài đặt giao dịch</b>\n"
                "Chọn tham số bên dưới để nhập giá trị mới, xem trước rồi xác nhận.\n"
                "Giới hạn số lệnh bao gồm vị thế đang mở và lệnh chờ.\n"
                "Giới hạn lỗ ngày tính toàn tài khoản theo ngày Việt Nam.\n\n"
                "Các tham số được chia theo nhóm bên dưới.\n\n"
                "Nâng cao: <code>/config set tên_tham_số giá_trị</code> cũng có bước xem trước.",
                parse_mode=ParseMode.HTML, reply_markup=build_risk_config_keyboard(cfg))
        except Exception as e:
            logger.error("cmd_config_failed", error=str(e), exc_info=True)
            await target.reply_text(f"❌ Lỗi xử lý cấu hình: <code>{e}</code>", parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_rules(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Display or reload Vietnamese keyword management rules."""
        target = update.effective_message or update.message
        if not target:
            return

        try:
            args = context.args or []
            if args and args[0].lower() == "reload":
                count = await self.service.load_management_rules_from_db()
                await target.reply_text(
                    f"🔄 <b>Đã tải lại {count} quy tắc từ cơ sở dữ liệu!</b>",
                    parse_mode=ParseMode.HTML,
                )
                return

            rules = self.service.get_management_rules()
            lines = [
                "📖 <b>Danh Sách Từ Khóa Quản Lý Lệnh</b>\n"
                ""
                "<i>Bot tự động nhận diện các từ khóa này trong tin nhắn Telegram:</i>\n"
            ]

            for r in rules:
                param_str = ""
                if "percentage" in r.default_params:
                    param_str = f"({r.default_params['percentage']}%)"
                elif "target" in r.default_params:
                    param_str = f"({r.default_params['target']})"

                action_name = r.action_type.value if hasattr(r.action_type, "value") else str(r.action_type)
                lines.append(f"• <code>{r.keyword}</code> ➔ <b>{action_name}</b> {param_str}")

            lines.extend([
                "\n"
                "💡 <b>Lệnh thao tác:</b>\n"
                "• Thêm/sửa: <code>/rule add &lt;từ khóa&gt; &lt;ACTION&gt; [params]</code>\n"
                "  <i>VD:</i> <code>/rule add \"cắt bớt\" PARTIAL_CLOSE 50</code>\n"
                "  <i>VD:</i> <code>/rule add \"về hòa\" BREAKEVEN</code>\n"
                "  <i>VD:</i> <code>/rule add \"chốt sạch\" CLOSE</code>\n"
                "• Xóa: <code>/rule del &lt;từ khóa&gt;</code>\n"
                "  <i>VD:</i> <code>/rule del \"chốt sạch\"</code>\n"
                "• Nạp lại: <code>/rules reload</code>\n\n"
                "<i>Hỗ trợ ACTION: PARTIAL_CLOSE, CLOSE, MOVE_SL, BREAKEVEN, REENTRY</i>"
            ])

            await target.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error("cmd_rules_failed", error=str(e), exc_info=True)
            await target.reply_text(f"❌ Lỗi: <code>{e}</code>", parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_rule(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Add, update, or delete Vietnamese keyword rules."""
        import shlex
        full_text = update.message.text or ""
        try:
            tokens = shlex.split(full_text)
        except Exception:
            tokens = full_text.split()

        if len(tokens) < 2:
            await update.message.reply_text(
                "⚠️ <b>Cú pháp quản lý từ khóa:</b>\n"
                ""
                "• Thêm: <code>/rule add &lt;từ khóa&gt; &lt;ACTION&gt; [params]</code>\n"
                "• Xóa: <code>/rule del &lt;từ khóa&gt;</code>\n\n"
                "<i>Xem danh sách: /rules</i>",
                parse_mode=ParseMode.HTML,
            )
            return

        subcmd = tokens[1].lower()

        if subcmd == "add":
            if len(tokens) < 4:
                await update.message.reply_text(
                    "❌ <b>Thiếu tham số:</b> <code>/rule add &lt;từ khóa&gt; &lt;ACTION&gt; [params]</code>\n"
                    "<i>Ví dụ:</i> <code>/rule add \"chốt 1/2\" PARTIAL_CLOSE 50</code>\n"
                    "<i>ACTIONS:</i> PARTIAL_CLOSE, CLOSE, MOVE_SL, BREAKEVEN, REENTRY",
                    parse_mode=ParseMode.HTML,
                )
                return

            keyword = tokens[2]
            action_type_str = tokens[3].upper()
            percentage = None
            target = None

            if len(tokens) >= 5:
                val = tokens[4]
                try:
                    percentage = Decimal(val.replace("%", ""))
                except Exception:
                    target = val.upper()

            ok = await self.service.add_or_update_management_rule(
                keyword=keyword,
                action_type_str=action_type_str,
                percentage=percentage,
                target=target,
            )

            if ok:
                await update.message.reply_text(
                    f"✅ <b>Đã Lưu Từ Khóa Quản Lý Lệnh</b>\n"
                    f""
                    f"• Từ khóa: <code>{keyword}</code>\n"
                    f"• Hành động: <b>{action_type_str}</b>\n"
                    f"• Tham số: {percentage or target or 'Mặc định'}\n\n"
                    f"<i>Đã cập nhật hệ thống và lưu trữ vào database.</i>",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await update.message.reply_text(
                    f"❌ <b>Hành động không hợp lệ:</b> <code>{action_type_str}</code>\n"
                    f"Hỗ trợ: PARTIAL_CLOSE, CLOSE, MOVE_SL, BREAKEVEN, REENTRY",
                    parse_mode=ParseMode.HTML,
                )

        elif subcmd in ("del", "delete", "remove"):
            if len(tokens) < 3:
                await update.message.reply_text(
                    "❌ <b>Thiếu từ khóa cần xóa:</b> <code>/rule del &lt;từ khóa&gt;</code>\n"
                    "<i>Ví dụ:</i> <code>/rule del \"chốt sạch\"</code>",
                    parse_mode=ParseMode.HTML,
                )
                return
            keyword = tokens[2]
            ok = await self.service.remove_management_rule(keyword)
            if ok:
                await update.message.reply_text(
                    f"🗑 <b>Đã Xóa Từ Khóa</b>\n"
                    f""
                    f"• Từ khóa: <code>{keyword}</code> đã được xóa khỏi hệ thống.",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await update.message.reply_text(
                    f"⚠️ Không tìm thấy từ khóa <code>{keyword}</code> trong hệ thống.",
                    parse_mode=ParseMode.HTML,
                )
        else:
            await update.message.reply_text(
                "❌ Lệnh không hợp lệ. Vui lòng dùng <code>add</code> hoặc <code>del</code>.",
                parse_mode=ParseMode.HTML,
            )

    @admin_only
    async def handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming text messages from admin. If a Telegram message link is found, replay it."""
        msg = update.effective_message
        if not msg or not msg.text:
            return

        routes = {"📊 Tổng quan": self.cmd_status, "📥 Tín hiệu": self.cmd_inbox,
                  "🗂 Kèo đang chạy": self.cmd_setups, "📋 Vị thế": self.cmd_positions,
                  "📈 Hiệu suất": self.cmd_stats, "⚙️ Cài đặt": self.cmd_config, "🏥 Kết nối": self.cmd_health}
        if msg.text in routes:
            context.user_data.pop("config_field", None)
            context.args = []
            await routes[msg.text](update, context)
            return
        pending = context.user_data.pop("config_field", None)
        if pending and pending["expires"] > time.monotonic():
            if msg.text.strip().lower() in {"hủy", "huy", "cancel"}:
                await msg.reply_text("Đã hủy nhập cấu hình.")
                return
            try:
                text, keyboard = self.prepare_config({pending["key"]: msg.text.strip()})
                await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            except (ValueError, TypeError) as exc:
                context.user_data["config_field"] = pending
                await msg.reply_text(f"Giá trị chưa hợp lệ: {str(exc)}\nNhập lại hoặc gửi 'hủy'.")
            return

        from app.telegram.utils import extract_telegram_link
        link = extract_telegram_link(msg.text)
        if link:
            await self._execute_replay_link(update, context, link)
            return

        raw = msg.text.strip().lower()
        if raw.startswith("/config") or raw.startswith("config") or any(
            line.strip().startswith("/config") or line.strip().startswith("config")
            for line in msg.text.splitlines()
        ):
            await self.cmd_config(update, context)
            return

    @admin_only
    async def cmd_replay(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Replay / re-process a trading signal by Telegram message link."""
        msg = update.effective_message
        if not msg:
            return

        text = msg.text or ""
        from app.telegram.utils import extract_telegram_link
        link = extract_telegram_link(text)

        if not link and context.args:
            link = extract_telegram_link(context.args[0])

        if not link:
            await msg.reply_text(
                "ℹ️ <b>Hướng Dẫn Nạp Lại Tín Hiệu (Replay)</b>\n"
                ""
                "Gửi link tin nhắn chứa tín hiệu từ kênh Telegram:\n"
                "• Cú pháp: <code>/replay &lt;link_tin_nhắn&gt;</code>\n"
                "  <i>VD:</i> <code>/replay https://t.me/MegaLodonGOLDForex/1241</code>\n\n"
                "• Hoặc dán trực tiếp link vào chat này mà không cần gõ lệnh.\n"
                "<i>Hỗ trợ cả kênh công khai (@slug) và kênh riêng tư (/c/channel_id).</i>",
                parse_mode=ParseMode.HTML,
            )
            return

        await self._execute_replay_link(update, context, link)

    async def _execute_replay_link(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, link: str
    ) -> None:
        """Fetch message from Telegram channel via Telethon userbot and run trading pipeline."""
        msg = update.effective_message
        if not msg:
            return

        status_msg = await msg.reply_text(
            f"⏳ <b>Đang tải tin nhắn từ Telegram...</b>\n"
            f""
            f"• Link: <code>{escape(link)}</code>",
            parse_mode=ParseMode.HTML,
        )

        try:
            from app.telegram.signal_listener import signal_listener
            fetch_res = await signal_listener.fetch_message_by_link(link)
            raw_text, msg_id, chat_id, canonical_link, is_reentry = fetch_res[:5]
            reply_to_msg_id = fetch_res[5] if len(fetch_res) > 5 else None

            signal_time = fetch_res[6] if len(fetch_res) > 6 else None
            if not isinstance(signal_time, datetime) or signal_time.tzinfo is None:
                raise ValueError("Thiếu thời gian gốc của tin nhắn; không cho phép thực thi")
            payload = {"raw_text": raw_text, "is_reentry": is_reentry, "message_id": msg_id,
                       "chat_id": chat_id, "message_link": canonical_link,
                       "reply_to_msg_id": reply_to_msg_id, "signal_time": signal_time}
            preview = await self.service.preview_signal(**payload)
            token = self.new_draft("signal", payload)
            text = preview["text"] + "\n\n<b>Nội dung gốc:</b>\n<pre>" + escape(raw_text[:800]) + "</pre>"
            await status_msg.edit_text(text, parse_mode=ParseMode.HTML,
                                       reply_markup=self.draft_keyboard(token, preview["can_execute"]))
        except Exception as e:
            logger.error("replay_signal_failed", link=link, error=str(e), exc_info=True)
            await status_msg.edit_text(
                f"❌ <b>Không thể nạp lại tín hiệu từ Telegram:</b>\n<code>{escape(str(e))}</code>",
                parse_mode=ParseMode.HTML,
            )

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Global error handler for Telegram application."""
        from telegram.error import NetworkError
        if isinstance(context.error, NetworkError):
            logger.debug("telegram_network_error_ignored")
            return
        if isinstance(context.error, BadRequest) and "Message is not modified" in str(context.error):
            logger.debug("telegram_message_not_modified_ignored")
            return
        logger.error("telegram_unhandled_exception", error=str(context.error), exc_info=context.error)

    # -------------------------------------------------------------
    # Interactive Callback Query Handler
    # -------------------------------------------------------------

    @admin_only
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query:
            return

        try:
            await query.answer()
        except Exception:
            pass

        data = query.data or ""
        if await self.handle_console_callback(update, context, data):
            return
        cfg = self.service.risk_config

        if data == "bot_pause":
            cfg.enabled = False
            await self.service.save_risk_config_to_db(updated_by="BUTTON")
            text, keyboard = await self.status_view()
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        elif data == "bot_resume":
            saved = await self.service.update_config_key("enabled", True, updated_by="BUTTON")
            if not saved:
                await query.message.reply_text("Không lưu được trạng thái; bot vẫn giữ cấu hình trước đó.")
            text, keyboard = await self.status_view()
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        elif data == "view_history":
            msg, keyboard = await self._format_history_message("today")
            await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        elif data.startswith("override_"):
            parts = data.split("_")
            action = parts[1]  # min, max, ignore
            try:
                sig_id = int(parts[2])
                base_html = (
                    query.message.text_html
                    .replace("Tin nhắn gốc:", "Signal:")
                    .replace("Tin nhắn gốc", "Signal")
                )
                if action == "ignore":
                    entry = self.service._pending_overrides.pop(sig_id, None)
                    if entry:
                        await self.service.set_signal_status(entry.get("db_id"), "REJECTED", "Người dùng bỏ qua")
                    # Just remove the keyboard and add a note
                    await query.edit_message_text(
                        f"{base_html}\n\n❌ <i>Đã bỏ qua lệnh chờ limit.</i>", 
                        parse_mode=ParseMode.HTML
                    )
                else:
                    res_msg = await self.service.execute_override_order(sig_id, action)
                    # Append result to the message and remove keyboard
                    await query.edit_message_text(
                        f"{base_html}\n\n{res_msg}",
                        parse_mode=ParseMode.HTML
                    )
            except (IndexError, ValueError):
                pass
        elif data.startswith("history_detail_"):
            try:
                _, _, days, page, ticket, deal_page = data.split("_")
                msg, keyboard = await self._format_history_detail(days, int(page), int(ticket), int(deal_page))
            except (ValueError, IndexError):
                return
            await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        elif data.startswith("history_"):
            try:
                days_filter = data.split("_")[1]
                history_period(days_filter)
            except (IndexError, ValueError):
                days_filter = "today"
            parts = data.split("_")
            try:
                page = int(parts[2]) if len(parts) > 2 else 0
            except ValueError:
                page = 0
            msg, keyboard = await self._format_history_message(days_filter, page)
            await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        elif data == "view_stats":
            msg, keyboard = await self._format_stats_message(7)
            await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        elif data.startswith("stats_"):
            try:
                days_filter = int(data.split("_")[1])
            except (IndexError, ValueError):
                days_filter = 7
            msg, keyboard = await self._format_stats_message(days_filter)
            await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        elif data == "status_refresh":
            text, keyboard = await self.status_view()
            try:
                await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            except BadRequest as exc:
                if "Message is not modified" not in str(exc):
                    raise


# Singleton Personal Bot instance
personal_bot = PersonalTelegramBot()
