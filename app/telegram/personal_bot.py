"""
Personal Telegram Bot — Admin Console & Control Plane.
"""

from __future__ import annotations

import dataclasses
import functools
import re
from decimal import Decimal
from typing import Any

import structlog
from telegram import InlineKeyboardMarkup, Update
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
                    f"━━━━━━━━━━━━━━━━\n"
                    f"User ID: <code>{user_id}</code>\n"
                    f"Chat ID: <code>{chat_id}</code>\n"
                    f"Admin ID: <code>{admin_id}</code>",
                    parse_mode=ParseMode.HTML,
                )
            return
        return await func(*args, **kwargs)

    return wrapper


class PersonalTelegramBot:
    """Manages the Admin Console Telegram Bot lifecycle."""

    def __init__(self, service: TradingService | None = None) -> None:
        self.service = service or trading_service
        self.app: Application | None = None
        self._running = False

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
        msg = (
            "━━━━━━━━━━━━━━━━\n"
            "🤖 <b>Trading Bot — Bảng Điều Khiển</b>\n\n"
            "Xin chào! Bạn có toàn quyền quản lý bot.\n\n"
            "📌 <b>Lệnh nhanh:</b>\n"
            "/status — Tổng quan hệ thống\n"
            "/positions — Vị thế đang mở\n"
            "/history — Lịch sử giao dịch\n"
            "/stats — Hiệu suất &amp; Win/Loss\n"
            "/balance — Số dư tài khoản\n"
            "/config — Cấu hình bot\n"
            "/help — Tất cả lệnh\n"
            "━━━━━━━━━━━━━━━━"
        )
        if update.effective_message:
            await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = (
            "📖 <b>Danh Mục Lệnh</b>\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "<b>📊 Giao dịch &amp; Vị thế:</b>\n"
            "/status — Tổng quan hệ thống\n"
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
            "━━━━━━━━━━━━━━━━"
        )
        if update.effective_message:
            await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        summary = await self.service.get_status_summary()
        status_icon = "🟢 HOẠT ĐỘNG" if summary["bot_enabled"] else "🔴 TẠM DỪNG"
        conn_icon = "✅ Đã kết nối" if summary["connected"] else "❌ Mất kết nối"
        now_vn = now_vietnam().strftime("%H:%M:%S %d/%m/%Y")

        msg = (
            f"📊 <b>Tổng Quan Hệ Thống</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"⏱ {now_vn}\n\n"
            f"<b>Bot:</b> {status_icon}\n"
            f"<b>MT5:</b> {conn_icon} — <code>{summary['server']}</code>\n"
            f"<b>Tài khoản:</b> <code>{summary['login']}</code>\n\n"
            f"<b>Số dư:</b> {summary['balance']} {summary['currency']}\n"
            f"<b>Equity:</b> {summary['equity']} {summary['currency']}\n"
            f"<b>Margin:</b> {summary['margin']} | <b>Khả dụng:</b> {summary['free_margin']}\n"
            f"<b>Vị thế mở:</b> {summary['open_positions_count']}\n"
            f"━━━━━━━━━━━━━━━━"
        )
        keyboard = build_status_keyboard(summary["bot_enabled"])
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    @admin_only
    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        positions = await self.service.broker.get_positions()
        if not positions:
            await update.message.reply_text("📂 <b>Không có vị thế đang mở.</b>", parse_mode=ParseMode.HTML)
            return

        lines = ["📂 <b>Vị Thế Đang Mở</b>\n━━━━━━━━━━━━━━━━\n"]
        for p in positions:
            pnl_icon = "🟢" if p.profit >= 0 else "🔴"
            open_time_str = format_vietnam_time(p.open_time, "%H:%M %d/%m")
            lines.append(
                f"{pnl_icon} <b>{p.symbol}</b> {p.position_type} {p.volume} lot\n"
                f"  Entry: <code>{p.open_price}</code> → Hiện tại: <code>{p.current_price}</code>\n"
                f"  PnL: <b>${p.profit:.2f}</b> | SL: {p.sl or '—'} | TP: {p.tp or '—'}\n"
                f"  Ticket: <code>{p.ticket}</code> | Mở: {open_time_str}"
            )

        keyboard = build_positions_keyboard(positions)
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=keyboard)

    @admin_only
    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        acc = await self.service.broker.get_account_info()
        msg = (
            f"💰 <b>Số Dư Tài Khoản</b>\n"
            f"━━━━━━━━━━━━━━━━\n\n"
            f"<b>Số dư:</b> {acc.balance} {acc.currency}\n"
            f"<b>Equity:</b> {acc.equity} {acc.currency}\n"
            f"<b>Margin:</b> {acc.margin}\n"
            f"<b>Khả dụng:</b> {acc.free_margin}\n"
            f"<b>Đòn bẩy:</b> 1:{acc.leverage}\n"
            f"<b>Server:</b> {acc.server}\n"
            f"━━━━━━━━━━━━━━━━"
        )
        if update.effective_message:
            await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)

    async def _format_history_message(self, days: int = 7) -> tuple[str, InlineKeyboardMarkup]:
        positions = await self.service.get_closed_positions(days=days, limit=15)
        if not positions:
            msg = (
                f"📜 <b>Lịch Sử Giao Dịch</b>\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"⏱ Kỳ xem: {days} ngày\n\n"
                f"Không có giao dịch nào trong khoảng thời gian này."
            )
        else:
            cards = []
            for pos in positions:
                badge_icon = "🔴" if pos.position_type == "SELL" else "🟢"
                pos_type_title = "Sell" if pos.position_type == "SELL" else "Buy"

                pnl_icon = "🟢" if pos.net_pnl > 0 else ("🔴" if pos.net_pnl < 0 else "⚪")
                if pos.net_pnl % Decimal("1") == Decimal("0"):
                    pnl_num_str = f"{pos.net_pnl:+,.0f}"
                else:
                    pnl_num_str = f"{pos.net_pnl:+,.2f}"
                pnl_str = f"{pnl_num_str} {pos.currency}"

                def _fmt_vol(v: Decimal) -> str:
                    s = f"{v:.2f}".rstrip("0")
                    if s.endswith("."):
                        s = s[:-1]
                    return s if s else "0"

                vol_str = f"{_fmt_vol(pos.volume)}/{_fmt_vol(pos.total_volume)} Lots"
                if not pos.is_fully_closed:
                    partial_note = " <i>(Đang giữ vị thế)</i>"
                elif pos.deals_count > 1:
                    partial_note = f" <i>({pos.deals_count} lần chốt)</i>"
                else:
                    partial_note = ""

                def _format_price_display(val: Decimal | None) -> str:
                    if val is None:
                        return "N/A"
                    min_decimals = 4 if val < Decimal("50") else 2
                    s = f"{val:,.5f}".rstrip("0")
                    if s.endswith("."):
                        s += "0" * min_decimals
                    else:
                        dec_len = len(s.split(".")[1])
                        if dec_len < min_decimals:
                            s += "0" * (min_decimals - dec_len)
                    return s

                open_price_str = _format_price_display(pos.open_price)
                close_price_str = _format_price_display(pos.close_price)
                close_time_str = format_vietnam_time(pos.close_time, "%d/%m/%Y %H:%M:%S")

                card_lines = [
                    f"{badge_icon} <b>{pos_type_title} {pos.symbol}</b>  (<code>#{pos.ticket}</code>)",
                    f"<b>Kết thúc PnL:</b> {pnl_icon} <b>{pnl_str}</b>",
                    f"<b>Đã đóng/Tổng số lượng:</b> <code>{vol_str}</code>{partial_note}",
                    f"<b>Giá mở:</b> <code>{open_price_str}</code>",
                    f"<b>Trung bình Giá đóng:</b> <code>{close_price_str}</code>",
                    f"<b>Thời gian đóng:</b> <code>{close_time_str}</code>",
                ]

                # Option 2: Hiển thị chi tiết từng lần đóng nếu có từ 2 lần chốt trở lên
                if pos.deals_count > 1 and pos.partial_deals:
                    card_lines.append("<b>Chi tiết các lần đóng:</b>")
                    for idx, pd in enumerate(pos.partial_deals):
                        prefix = "  └" if idx == len(pos.partial_deals) - 1 else "  ├"
                        if pd.profit % Decimal("1") == Decimal("0"):
                            pd_pnl_str = f"{pd.profit:+,.0f} {pos.currency}"
                        else:
                            pd_pnl_str = f"{pd.profit:+,.2f} {pos.currency}"
                        pd_vol_str = _fmt_vol(pd.volume)
                        pd_price_str = _format_price_display(pd.price)
                        card_lines.append(
                            f"{prefix} <code>{pd_vol_str} lot @ {pd_price_str} ({pd_pnl_str})</code>"
                        )

                card = "\n".join(card_lines)
                cards.append(card)

            msg = (
                f"📜 <b>Lịch Sử Giao Dịch ({days} ngày)</b>\n"
                f"━━━━━━━━━━━━━━━━\n\n"
                + "\n\n────────────────\n\n".join(cards)
                + f"\n\n━━━━━━━━━━━━━━━━\n📊 Hiển thị {len(positions)} vị thế gần nhất."
            )

        keyboard = build_history_keyboard(days)
        return msg, keyboard

    async def _format_stats_message(self, days: int = 7) -> tuple[str, InlineKeyboardMarkup]:
        stats = await self.service.get_performance_summary(days=days)
        net_pnl = stats["net_pnl"]
        pnl_icon = "🟢" if net_pnl > Decimal("0") else ("🔴" if net_pnl < Decimal("0") else "⚪")
        pnl_sign = "+" if net_pnl > Decimal("0") else ""

        pf_val = stats["profit_factor"]
        pf_str = f"{pf_val:.2f}" if pf_val < 900 else "∞ (Chưa có lệnh thua)"

        msg = (
            f"📈 <b>Báo Cáo Hiệu Suất</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"⏱ Kỳ thống kê: {days} ngày\n\n"
            f"<b>Tổng lệnh:</b> {stats['total_trades']}\n"
            f"✅ Thắng: {stats['win_trades']} (<b>{stats['win_rate']}%</b>)\n"
            f"❌ Thua: {stats['loss_trades']}\n"
            f"⚪ Hòa vốn: {stats['breakeven_trades']}\n\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"<b>Tổng lãi:</b> +${stats['total_profit']:.2f}\n"
            f"<b>Tổng lỗ:</b> -${abs(stats['total_loss']):.2f}\n"
            f"<b>Profit Factor:</b> <code>{pf_str}</code>\n"
            f"<b>Lãi ròng:</b> {pnl_icon} <b>{pnl_sign}${net_pnl:.2f}</b>\n\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"<b>Thắng lớn nhất:</b> +${stats['best_trade']:.2f}\n"
            f"<b>Thua lớn nhất:</b> ${stats['worst_trade']:.2f}"
        )
        keyboard = build_stats_keyboard(days)
        return msg, keyboard

    @admin_only
    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        days = 7
        if context.args:
            try:
                days = max(1, int(context.args[0]))
            except ValueError:
                pass
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
                    f"━━━━━━━━━━━━━━━━\n"
                    f"• Ticket: <code>{ticket}</code>\n"
                    f"• Kết quả: Thành công",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await update.message.reply_text(
                    f"❌ <b>Đóng Vị Thế Thất Bại</b>\n"
                    f"━━━━━━━━━━━━━━━━\n"
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
            f"━━━━━━━━━━━━━━━━\n"
            f"• Đã đóng thành công: <b>{successes}/{len(results)}</b> vị thế",
            parse_mode=ParseMode.HTML,
        )

    @admin_only
    async def cmd_partial(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        if not args:
            await update.message.reply_text(
                "⚠️ <b>Cú pháp lệnh chốt bớt (Partial Close):</b>\n"
                "━━━━━━━━━━━━━━━━\n"
                "• <b>Chốt tất cả vị thế:</b>\n"
                "  <code>/partial &lt;%&gt;</code>\n"
                "  <i>Ví dụ:</i> <code>/partial 50</code> (chốt 50% tất cả vị thế đang mở)\n\n"
                "• <b>Chốt tất cả vị thế của 1 cặp:</b>\n"
                "  <code>/partial &lt;%&gt; &lt;symbol&gt;</code>\n"
                "  <i>Ví dụ:</i> <code>/partial 50 XAU</code>\n\n"
                "• <b>Chốt 1 lệnh cụ thể:</b>\n"
                "  <code>/partial &lt;ticket&gt; &lt;%&gt;</code>\n"
                "  <i>Ví dụ:</i> <code>/partial 342003626 50</code>\n"
                "━━━━━━━━━━━━━━━━\n"
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

            lines = [f"✂️ <b>Chốt Bớt {percent}% Cho Tất Cả Vị Thế</b>\n━━━━━━━━━━━━━━━━"]
            for pos, res, vol in results:
                icon = "✅" if res.success else "❌"
                rem = max(Decimal("0.00"), pos.volume - vol) if res.success else pos.volume
                status_text = f"Đã đóng: <b>{vol} lot</b> (còn {rem} lot)" if res.success else f"Lỗi: {res.comment}"
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

                lines = [f"✂️ <b>Chốt Bớt {percent_first}% Vị Thế ({symbol_filter.upper()})</b>\n━━━━━━━━━━━━━━━━"]
                for pos, res, vol in results:
                    icon = "✅" if res.success else "❌"
                    rem = max(Decimal("0.00"), pos.volume - vol) if res.success else pos.volume
                    status_text = f"Đã đóng: <b>{vol} lot</b> (còn {rem} lot)" if res.success else f"Lỗi: {res.comment}"
                    lines.append(f"{icon} Ticket <code>{pos.ticket}</code> ({pos.symbol}): {status_text}")

                await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
                return
        except Exception:
            pass

        # Otherwise: /partial <ticket> <percent> (e.g. /partial 342003626 50)
        try:
            ticket = int(arg1)
            percent = Decimal(arg2.replace("%", ""))
            positions = await self.service.broker.get_positions()
            target = next((p for p in positions if p.ticket == ticket), None)
            if not target:
                await update.message.reply_text(f"❌ Không tìm thấy vị thế <code>{ticket}</code>.", parse_mode=ParseMode.HTML)
                return

            vol = (target.volume * (percent / Decimal("100"))).quantize(Decimal("0.01"))
            vol = max(Decimal("0.01"), min(vol, target.volume))

            res = await self.service.close_position_by_ticket(ticket, volume=vol)
            if res.success:
                rem_vol = target.volume - vol
                await update.message.reply_text(
                    f"✅ <b>Chốt Lời Một Phần Thành Công</b>\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"• Ticket: <code>{ticket}</code> ({target.symbol})\n"
                    f"• Đã đóng: <b>{vol} lot</b> ({percent}%)\n"
                    f"• Còn lại: <b>{rem_vol} lot</b>",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await update.message.reply_text(
                    f"❌ <b>Chốt Lời Thất Bại</b>\n"
                    f"━━━━━━━━━━━━━━━━\n"
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
                    f"━━━━━━━━━━━━━━━━\n"
                    f"• Ticket: <code>{ticket}</code>\n"
                    f"• SL mới: <code>{price}</code>",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await update.message.reply_text(
                    f"❌ <b>Cập Nhật SL Thất Bại</b>\n"
                    f"━━━━━━━━━━━━━━━━\n"
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
                    f"━━━━━━━━━━━━━━━━\n"
                    f"• Ticket: <code>{ticket}</code>\n"
                    f"• TP mới: <code>{price}</code>",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await update.message.reply_text(
                    f"❌ <b>Cập Nhật TP Thất Bại</b>\n"
                    f"━━━━━━━━━━━━━━━━\n"
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
            if res.success:
                await update.message.reply_text(
                    f"🎯 <b>Đã Dời SL Về Hòa Vốn (Entry)</b>\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"• Ticket: <code>{ticket}</code>",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await update.message.reply_text(
                    f"❌ <b>Dời SL Thất Bại</b>\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"• Ticket: <code>{ticket}</code>\n"
                    f"• Lý do: {res.comment}",
                    parse_mode=ParseMode.HTML,
                )
        except Exception as e:
            await update.message.reply_text(f"❌ Lỗi: <code>{e}</code>", parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.service.update_config_key("enabled", False, updated_by="PAUSE")
        now_vn = now_vietnam().strftime("%H:%M:%S %d/%m/%Y")
        await update.message.reply_text(
            f"⏸️ <b>Đã Tạm Dừng Bot</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
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
            f"━━━━━━━━━━━━━━━━\n"
            f"• Trạng thái: <b>HOẠT ĐỘNG (Active)</b>\n"
            f"• Bot sẵn sàng nhận và xử lý tín hiệu mới.\n"
            f"⏱ {now_vn}",
            parse_mode=ParseMode.HTML,
        )

    @admin_only
    async def cmd_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        connected = await self.service.broker.is_connected()
        status_broker = "✅ Hoạt động bình thường" if connected else "❌ Mất kết nối"
        now_vn = now_vietnam().strftime("%H:%M:%S %d/%m/%Y")
        msg = (
            f"🏥 <b>Kiểm Tra Sức Khỏe Hệ Thống</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"⏱ {now_vn}\n\n"
            f"• Kết nối MT5: {status_broker}\n"
            f"• Nhận tín hiệu (Listener): ✅ Sẵn sàng\n"
            f"• Quản lý rủi ro: ✅ Đang chạy\n"
            f"• Cặp giao dịch: <code>{', '.join(self.service.risk_config.allowed_symbols)}</code>\n"
            f"━━━━━━━━━━━━━━━━"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_kill(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.service.update_config_key("enabled", False, updated_by="KILL_SWITCH")
        results = await self.service.close_all_positions()
        now_vn = now_vietnam().strftime("%H:%M:%S %d/%m/%Y")
        await update.message.reply_text(
            f"🚨 <b>KÍCH HOẠT DỪNG KHẨN CẤP (KILL SWITCH)</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"• Trạng thái Bot: <b>ĐÃ TẮT</b> (Đã lưu DB)\n"
            f"• Đã đóng: <b>{len(results)}</b> vị thế đang mở\n"
            f"⏱ {now_vn}",
            parse_mode=ParseMode.HTML,
        )

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
                updates_dict = {k.lower(): v for k, v in pairs}
                results = await self.service.update_config_keys(updates_dict, updated_by="ADMIN")

                success_lines = []
                failed_lines = []
                for k, ok in results.items():
                    if ok:
                        val = getattr(cfg, k, updates_dict[k])
                        success_lines.append(f"• <code>{k}</code> = <b>{val}</b>")
                    else:
                        failed_lines.append(f"• <code>{k}</code>: Không tìm thấy thuộc tính hoặc giá trị sai")

                response_parts = []
                if success_lines:
                    count_str = f" {len(success_lines)}" if len(success_lines) > 1 else ""
                    response_parts.append(
                        f"✅ <b>Đã cập nhật{count_str} cấu hình vào DB:</b>\n" + "\n".join(success_lines)
                    )
                if failed_lines:
                    response_parts.append(
                        f"❌ <b>Cấu hình không hợp lệ:</b>\n" + "\n".join(failed_lines) +
                        "\n<i>Gõ <code>/config</code> để xem danh sách tham số chuẩn.</i>"
                    )

                if response_parts:
                    await target.reply_text("\n\n".join(response_parts), parse_mode=ParseMode.HTML)
                    return

            syms_str = ", ".join(cfg.allowed_symbols)
            msg = (
                "⚙️ <b>Live Risk & Management Configuration</b>\n"
                "<i>Cấu hình động lưu trữ trực tiếp vào PostgreSQL.</i>\n\n"
                "<b>1. 🎯 Vào lệnh & Dual Entry (Min / Max Entry):</b>\n"
                f"• Dual Entry: <b>{'ON' if cfg.enable_dual_entry else 'OFF'}</b> ➔ <code>/config set enable_dual_entry {str(not cfg.enable_dual_entry).lower()}</code>\n"
                f"• Lot Min Entry (Market): <b>{cfg.lot_min_entry}</b> ➔ <code>/config set lot_min_entry {cfg.lot_min_entry}</code>\n"
                f"• Lot Max Entry (Limit): <b>{cfg.lot_max_entry}</b> ➔ <code>/config set lot_max_entry {cfg.lot_max_entry}</code>\n"
                f"• Dung sai trượt giá: <b>{cfg.max_slippage_entry} giá</b> ➔ <code>/config set max_slippage_entry {cfg.max_slippage_entry}</code>\n\n"
                "<b>2. 🛡️ Quản lý rủi ro & Tài khoản (Risk & Limits):</b>\n"
                f"• Bot Active: <b>{'ON' if cfg.enabled else 'OFF'}</b> ➔ <code>/config set enabled {str(not cfg.enabled).lower()}</code>\n"
                f"• Lệnh mở tối đa: <b>{cfg.max_open_positions}</b> ➔ <code>/config set max_open_positions {cfg.max_open_positions}</code>\n"
                f"• Lot mặc định: <b>{cfg.default_lot}</b> ➔ <code>/config set default_lot {cfg.default_lot}</code>\n"
                f"• Lot tối thiểu: <b>{cfg.min_lot}</b> ➔ <code>/config set min_lot {cfg.min_lot}</code>\n"
                f"• Lot tối đa: <b>{cfg.max_lot}</b> ➔ <code>/config set max_lot {cfg.max_lot}</code>\n"
                f"• Lỗ tối đa ngày: <b>{cfg.max_daily_loss_percent}%</b> ➔ <code>/config set max_daily_loss_percent {cfg.max_daily_loss_percent}</code>\n"
                f"• Bắt buộc Stop Loss: <b>{cfg.require_sl}</b> ➔ <code>/config set require_sl {str(not cfg.require_sl).lower()}</code>\n"
                f"• Bắt buộc Take Profit: <b>{cfg.require_tp}</b> ➔ <code>/config set require_tp {str(not cfg.require_tp).lower()}</code>\n\n"
                "<b>3. ✂️ Quản lý vị thế & Chốt lời (Position Management):</b>\n"
                f"• Chốt lời 1 phần: <b>{cfg.default_partial_close_percent}%</b> ➔ <code>/config set default_partial_close_percent {cfg.default_partial_close_percent}</code>\n"
                f"• Tự dời SL về Entry: <b>{cfg.auto_breakeven_on_partial}</b> ➔ <code>/config set auto_breakeven_on_partial {str(not cfg.auto_breakeven_on_partial).lower()}</code>\n"
                f"• Bù giá dời SL (Offset): <b>{cfg.breakeven_offset_price}</b> ➔ <code>/config set breakeven_offset_price 0.0</code>\n"
                f"• Cho phép chốt nhiều lần: <b>{cfg.allow_multiple_partial_closes}</b> ➔ <code>/config set allow_multiple_partial_closes {str(not cfg.allow_multiple_partial_closes).lower()}</code>\n\n"
                "<b>4. ⏳ Cặp tiền, Thời gian & Hạn tín hiệu (Timing & Symbols):</b>\n"
                f"• Cặp cho phép: <b>{syms_str}</b>\n"
                f"  ➔ <code>/config set allowed_symbols {syms_str}</code>\n"
                f"• Lệch giờ sàn (Broker Offset): <b>UTC+{cfg.broker_utc_offset_hours}</b> (Giờ VN: GMT+7)\n"
                f"  ➔ <code>/config set broker_utc_offset_hours {cfg.broker_utc_offset_hours}</code>\n"
                f"• Hạn tín hiệu (Expire): <b>{cfg.signal_expire_seconds}s</b> ➔ <code>/config set signal_expire_seconds 300</code>\n"
                f"• Lọc trùng (Dedup): <b>{cfg.duplicate_timeout_seconds}s</b> ➔ <code>/config set duplicate_timeout_seconds 10</code>\n\n"
                "🔄 <i>Nạp lại từ DB:</i> <code>/config reload</code>\n"
                "💡 <i>Chạm vào bất kỳ câu lệnh nào ở trên để copy nhanh và gửi!</i>"
            )
            keyboard = build_risk_config_keyboard(cfg)
            await target.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)
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
                "━━━━━━━━━━━━━━━━\n"
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
                "\n━━━━━━━━━━━━━━━━\n"
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
                "━━━━━━━━━━━━━━━━\n"
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
                    f"━━━━━━━━━━━━━━━━\n"
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
                    f"━━━━━━━━━━━━━━━━\n"
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
                "━━━━━━━━━━━━━━━━\n"
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
            f"━━━━━━━━━━━━━━━━\n"
            f"• Link: <code>{link}</code>",
            parse_mode=ParseMode.HTML,
        )

        try:
            from app.telegram.signal_listener import signal_listener
            fetch_res = await signal_listener.fetch_message_by_link(link)
            raw_text, msg_id, chat_id, canonical_link, is_reentry = fetch_res[:5]
            reply_to_msg_id = fetch_res[5] if len(fetch_res) > 5 else None

            preview_text = raw_text[:400] + ("..." if len(raw_text) > 400 else "")
            reply_info = f"\n• <b>Reply to:</b> <code>#{reply_to_msg_id}</code>" if reply_to_msg_id else ""
            await status_msg.edit_text(
                f"📥 <b>Đã Tải Tin Nhắn! Đang xử lý...</b>\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"• <b>Kênh / Chat ID:</b> <code>{chat_id}</code>\n"
                f"• <b>Message ID:</b> <code>#{msg_id}</code>{reply_info}\n\n"
                f"<b>Nội dung:</b>\n<pre>{preview_text}</pre>",
                parse_mode=ParseMode.HTML,
            )

            # Route through TradingService full pipeline
            await self.service.process_raw_message(
                raw_text,
                is_reentry=is_reentry,
                message_id=msg_id,
                chat_id=chat_id,
                message_link=canonical_link,
                reply_to_msg_id=reply_to_msg_id,
            )
        except Exception as e:
            logger.error("replay_signal_failed", link=link, error=str(e), exc_info=True)
            await status_msg.edit_text(
                f"❌ <b>Không thể nạp lại tín hiệu từ Telegram:</b>\n<code>{e}</code>",
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
        cfg = self.service.risk_config

        if data == "bot_pause":
            cfg.enabled = False
            await self.service.save_risk_config_to_db(updated_by="BUTTON")
            await query.edit_message_reply_markup(reply_markup=build_status_keyboard(False))
        elif data == "bot_resume":
            cfg.enabled = True
            await self.service.save_risk_config_to_db(updated_by="BUTTON")
            await query.edit_message_reply_markup(reply_markup=build_status_keyboard(True))
        elif data == "toggle_bot_enabled":
            cfg.enabled = not cfg.enabled
            await self.service.save_risk_config_to_db(updated_by="BUTTON")
            await query.edit_message_reply_markup(reply_markup=build_risk_config_keyboard(cfg))
        elif data == "toggle_require_sl":
            cfg.require_sl = not cfg.require_sl
            await self.service.save_risk_config_to_db(updated_by="BUTTON")
            await query.edit_message_reply_markup(reply_markup=build_risk_config_keyboard(cfg))
        elif data == "toggle_auto_be":
            cfg.auto_breakeven_on_partial = not cfg.auto_breakeven_on_partial
            await self.service.save_risk_config_to_db(updated_by="BUTTON")
            await query.edit_message_reply_markup(reply_markup=build_risk_config_keyboard(cfg))
        elif data == "toggle_dual_entry":
            cfg.enable_dual_entry = not cfg.enable_dual_entry
            await self.service.save_risk_config_to_db(updated_by="BUTTON")
            await query.edit_message_reply_markup(reply_markup=build_risk_config_keyboard(cfg))
        elif data == "cycle_max_slippage":
            slips = [Decimal("0.5"), Decimal("1.0"), Decimal("1.5"), Decimal("2.0"), Decimal("3.0")]
            try:
                curr_idx = slips.index(cfg.max_slippage_entry)
                cfg.max_slippage_entry = slips[(curr_idx + 1) % len(slips)]
            except ValueError:
                cfg.max_slippage_entry = Decimal("1.0")
            await self.service.save_risk_config_to_db(updated_by="BUTTON")
            await query.edit_message_reply_markup(reply_markup=build_risk_config_keyboard(cfg))
        elif data == "cycle_lot_min_entry":
            lots = [Decimal("0.01"), Decimal("0.02"), Decimal("0.03"), Decimal("0.05"), Decimal("0.10")]
            try:
                curr_idx = lots.index(cfg.lot_min_entry)
                cfg.lot_min_entry = lots[(curr_idx + 1) % len(lots)]
            except ValueError:
                cfg.lot_min_entry = Decimal("0.01")
            await self.service.save_risk_config_to_db(updated_by="BUTTON")
            await query.edit_message_reply_markup(reply_markup=build_risk_config_keyboard(cfg))
        elif data == "cycle_lot_max_entry":
            lots = [Decimal("0.01"), Decimal("0.02"), Decimal("0.03"), Decimal("0.05"), Decimal("0.10")]
            try:
                curr_idx = lots.index(cfg.lot_max_entry)
                cfg.lot_max_entry = lots[(curr_idx + 1) % len(lots)]
            except ValueError:
                cfg.lot_max_entry = Decimal("0.01")
            await self.service.save_risk_config_to_db(updated_by="BUTTON")
            await query.edit_message_reply_markup(reply_markup=build_risk_config_keyboard(cfg))
        elif data == "edit_max_pos":
            positions_list = [1, 2, 3, 5, 10]
            try:
                curr_idx = positions_list.index(cfg.max_open_positions)
                cfg.max_open_positions = positions_list[(curr_idx + 1) % len(positions_list)]
            except ValueError:
                cfg.max_open_positions = 5
            await self.service.save_risk_config_to_db(updated_by="BUTTON")
            await query.edit_message_reply_markup(reply_markup=build_risk_config_keyboard(cfg))
        elif data == "toggle_partial_pct":
            pcts = [Decimal("30.0"), Decimal("50.0"), Decimal("70.0")]
            try:
                curr_idx = pcts.index(cfg.default_partial_close_percent)
                cfg.default_partial_close_percent = pcts[(curr_idx + 1) % len(pcts)]
            except ValueError:
                cfg.default_partial_close_percent = Decimal("50.0")
            await self.service.save_risk_config_to_db(updated_by="BUTTON")
            await query.edit_message_reply_markup(reply_markup=build_risk_config_keyboard(cfg))
        elif data.startswith("close_"):
            ticket_str = data.split("_")[1]
            if ticket_str == "all":
                await self.service.close_all_positions()
                await query.edit_message_text("🚨 <b>Đã đóng toàn bộ vị thế.</b>", parse_mode=ParseMode.HTML)
            else:
                try:
                    ticket = int(ticket_str)
                    res = await self.service.close_position_by_ticket(ticket)
                    if res.success:
                        await query.edit_message_text(f"✅ Đã đóng vị thế <code>{ticket}</code>.", parse_mode=ParseMode.HTML)
                    else:
                        await query.edit_message_text(f"❌ Không thể đóng vị thế <code>{ticket}</code>: {res.comment}", parse_mode=ParseMode.HTML)
                except ValueError:
                    pass
        elif data.startswith("be_"):
            ticket = int(data.split("_")[1])
            res = await self.service.move_to_breakeven(ticket)
            if res.success:
                await query.edit_message_text(f"🎯 Đã dời SL vị thế <code>{ticket}</code> về Entry!", parse_mode=ParseMode.HTML)
            else:
                await query.edit_message_text(f"❌ Không thể dời SL: {res.comment}", parse_mode=ParseMode.HTML)
        elif data.startswith("partial_"):
            parts = data.split("_")
            if parts[1] == "all":
                pct = Decimal(parts[2]) if len(parts) > 2 else Decimal("50")
                results = await self.service.partial_close_positions(percentage=pct)
                if not results:
                    await query.edit_message_text("📂 <b>Không có vị thế nào đang mở.</b>", parse_mode=ParseMode.HTML)
                else:
                    succ = sum(1 for _, r, _ in results if r.success)
                    await query.edit_message_text(f"✂️ <b>Đã chốt {pct}% cho {succ}/{len(results)} vị thế.</b>", parse_mode=ParseMode.HTML)
            else:
                try:
                    ticket = int(parts[1])
                    pct = Decimal(parts[2]) if len(parts) > 2 else Decimal("50")
                    positions = await self.service.broker.get_positions()
                    target = next((p for p in positions if p.ticket == ticket), None)
                    if not target:
                        await query.edit_message_text(f"❌ Không tìm thấy vị thế <code>{ticket}</code>.", parse_mode=ParseMode.HTML)
                    else:
                        vol = (target.volume * (pct / Decimal("100"))).quantize(Decimal("0.01"))
                        vol = max(Decimal("0.01"), min(vol, target.volume))
                        res = await self.service.close_position_by_ticket(ticket, volume=vol)
                        if res.success:
                            await query.edit_message_text(f"✅ Đã chốt {pct}% ({vol} lot) vị thế <code>{ticket}</code>.", parse_mode=ParseMode.HTML)
                        else:
                            await query.edit_message_text(f"❌ Chốt vị thế <code>{ticket}</code> thất bại: {res.comment}", parse_mode=ParseMode.HTML)
                except Exception as e:
                    await query.edit_message_text(f"❌ Lỗi: <code>{e}</code>", parse_mode=ParseMode.HTML)
        elif data in ("view_positions", "positions_refresh"):
            positions = await self.service.broker.get_positions()
            if not positions:
                keyboard = build_status_keyboard(cfg.enabled)
                await query.edit_message_text("📂 <b>Không có vị thế đang mở.</b>", parse_mode=ParseMode.HTML, reply_markup=keyboard)
            else:
                lines = ["📂 <b>Vị Thế Đang Mở</b>\n━━━━━━━━━━━━━━━━\n"]
                for p in positions:
                    pnl_icon = "🟢" if p.profit >= Decimal("0") else "🔴"
                    open_time_str = format_vietnam_time(p.open_time, "%H:%M %d/%m")
                    lines.append(
                        f"{pnl_icon} <b>{p.symbol}</b> {p.position_type} {p.volume} lot\n"
                        f"  Entry: <code>{p.open_price}</code> → Hiện tại: <code>{p.current_price}</code>\n"
                        f"  PnL: <b>${p.profit:.2f}</b> | SL: {p.sl or '—'} | TP: {p.tp or '—'}\n"
                        f"  Ticket: <code>{p.ticket}</code> | Mở: {open_time_str}"
                    )
                keyboard = build_positions_keyboard(positions)
                await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=keyboard)
        elif data == "view_orders":
            from app.telegram.keyboards import build_pending_orders_keyboard
            orders = await self.service.broker.get_pending_orders()
            if not orders:
                keyboard = build_status_keyboard(cfg.enabled)
                await query.edit_message_text("⏳ <b>Không có lệnh chờ nào.</b>", parse_mode=ParseMode.HTML, reply_markup=keyboard)
            else:
                lines = ["⏳ <b>Danh Sách Lệnh Chờ</b>\n━━━━━━━━━━━━━━━━\n"]
                for o in orders:
                    lines.append(
                        f"• <b>{o.symbol}</b> {o.order_type} {o.volume} lot @ <code>{o.price}</code>\n"
                        f"  SL: {o.sl or '—'} | TP: {o.tp or '—'} | Ticket: <code>{o.ticket}</code>"
                    )
                keyboard = build_pending_orders_keyboard(orders)
                await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=keyboard)
        elif data.startswith("cancel_") and data != "cancel_all_orders":
            ticket = int(data.split("_")[1])
            res = await self.service.broker.cancel_order(ticket)
            if res.success:
                await query.edit_message_text(f"✅ Đã hủy lệnh chờ <code>{ticket}</code>.", parse_mode=ParseMode.HTML)
            else:
                await query.edit_message_text(f"❌ Không thể hủy lệnh chờ <code>{ticket}</code>: {res.comment}", parse_mode=ParseMode.HTML)
        elif data == "cancel_all_orders":
            orders = await self.service.broker.get_pending_orders()
            results = []
            for o in orders:
                res = await self.service.broker.cancel_order(o.ticket)
                if res.success:
                    results.append(f"• Ticket <code>{o.ticket}</code>: ✅ Đã hủy")
                else:
                    results.append(f"• Ticket <code>{o.ticket}</code>: ❌ Lỗi ({res.comment})")
            if results:
                msg = "🗑 <b>Kết Quả Hủy Lệnh Chờ:</b>\n━━━━━━━━━━━━━━━━\n" + "\n".join(results)
            else:
                msg = "⏳ <b>Không có lệnh chờ nào.</b>"
            await query.edit_message_text(msg, parse_mode=ParseMode.HTML)
        elif data == "view_history":
            msg, keyboard = await self._format_history_message(7)
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
        elif data.startswith("history_"):
            try:
                days_filter = int(data.split("_")[1])
            except (IndexError, ValueError):
                days_filter = 7
            msg, keyboard = await self._format_history_message(days_filter)
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
        elif data == "view_config_risk":
            keyboard = build_risk_config_keyboard(cfg)
            await query.edit_message_reply_markup(reply_markup=keyboard)
        elif data == "status_refresh":
            summary = await self.service.get_status_summary()
            status_icon = "🟢 HOẠT ĐỘNG" if summary["bot_enabled"] else "🔴 TẠM DỪNG"
            conn_icon = "✅ Đã kết nối" if summary["connected"] else "❌ Mất kết nối"
            now_vn = now_vietnam().strftime("%H:%M:%S %d/%m/%Y")
            msg = (
                f"📊 <b>Tổng Quan Hệ Thống</b>\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"⏱ {now_vn}\n\n"
                f"<b>Bot:</b> {status_icon}\n"
                f"<b>MT5:</b> {conn_icon} — <code>{summary['server']}</code>\n"
                f"<b>Tài khoản:</b> <code>{summary['login']}</code>\n\n"
                f"<b>Số dư:</b> {summary['balance']} {summary['currency']}\n"
                f"<b>Equity:</b> {summary['equity']} {summary['currency']}\n"
                f"<b>Margin:</b> {summary['margin']} | <b>Khả dụng:</b> {summary['free_margin']}\n"
                f"<b>Vị thế mở:</b> {summary['open_positions_count']}\n"
                f"<b>Lệnh chờ:</b> {summary.get('pending_orders_count', 0)}\n"
                f"━━━━━━━━━━━━━━━━"
            )
            keyboard = build_status_keyboard(summary["bot_enabled"])
            try:
                await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise


# Singleton Personal Bot instance
personal_bot = PersonalTelegramBot()
