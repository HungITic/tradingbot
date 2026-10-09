"""
Personal Telegram Bot — Admin Console & Control Plane.
"""

from __future__ import annotations

import functools
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
)

from app.config.settings import settings
from app.telegram.keyboards import (
    build_history_keyboard,
    build_positions_keyboard,
    build_risk_config_keyboard,
    build_stats_keyboard,
    build_status_keyboard,
)
from app.trading.service import TradingService, trading_service

logger = structlog.get_logger(__name__)


def admin_only(func):
    """Decorator ensuring only the authorized admin chat can trigger commands."""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any):
        update: Update | None = None
        for arg in args:
            if isinstance(arg, Update):
                update = arg
                break
        if not update:
            update = kwargs.get("update")

        chat_id = update.effective_chat.id if (update and update.effective_chat) else 0
        if chat_id != settings.telegram.admin_chat_id:
            logger.warning("unauthorized_access_attempt", chat_id=chat_id)
            if update and update.effective_message:
                await update.effective_message.reply_text("⛔ Unauthorized access.")
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
                await self.app.updater.start_polling(drop_pending_updates=True)
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
            "🤖 <b>Trading Bot V1 — Admin Console</b>\n\n"
            "Welcome! You have full control plane access.\n\n"
            "<b>Quick Commands:</b>\n"
            "• /status — Tổng quan hệ thống & tài khoản\n"
            "• /positions — Danh sách lệnh đang mở & nút thao tác\n"
            "• /history — Lịch sử lệnh đã đóng (P&amp;L từng lệnh)\n"
            "• /stats — Báo cáo hiệu suất & tỷ lệ Win/Loss\n"
            "• /balance — Số dư, Equity, Margin\n"
            "• /config — Cấu hình rủi ro & bot\n"
            "• /rules — Quản lý từ khóa tiếng Việt (Keywords)\n"
            "• /help — Toàn bộ danh mục lệnh"
        )
        if update.effective_message:
            await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = (
            "📖 <b>Command Reference</b>\n\n"
            "<b>📊 Trading & Positions:</b>\n"
            "/status — Live overview & quick buttons\n"
            "/positions — Open positions with interactive buttons\n"
            "/history [days] — Xem lịch sử lệnh đã đóng (mặc định 7 ngày, VD: <code>/history 30</code>)\n"
            "/stats [days] — Thống kê Win/Loss, Win Rate, Profit Factor (VD: <code>/stats 30</code>)\n"
            "/balance — Live balance and equity\n"
            "/close &lt;ticket&gt; — Close specific position\n"
            "/closeall — Emergency close all open positions\n"
            "/partial &lt;ticket&gt; &lt;%&gt; — Partial close (e.g. <code>/partial 12345 50</code>)\n"
            "/setsl &lt;ticket&gt; &lt;price&gt; — Update Stop Loss\n"
            "/settp &lt;ticket&gt; &lt;price&gt; — Update Take Profit\n"
            "/breakeven &lt;ticket&gt; — Move SL to entry price\n\n"
            "<b>📝 Quản lý từ khóa tiếng Việt (Rules):</b>\n"
            "/rules — Xem toàn bộ từ khóa quản lý lệnh\n"
            "/rule add &lt;từ khóa&gt; &lt;action&gt; [params] — Thêm từ khóa\n"
            "/rule del &lt;từ khóa&gt; — Xóa từ khóa\n"
            "/rules reload — Nạp lại từ PostgreSQL\n\n"
            "<b>⚙️ Dynamic Configuration:</b>\n"
            "/config — View all settings\n"
            "/config risk — Interactive Risk tuning buttons\n"
            "/config set &lt;key&gt; &lt;value&gt; — Update any parameter instantly\n\n"
            "<b>🛑 Bot Control:</b>\n"
            "/pause — Temporarily pause signal execution\n"
            "/resume — Resume trading\n"
            "/health — Health check MT5 & database\n"
            "/kill — Emergency disable all operations"
        )
        if update.effective_message:
            await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        summary = await self.service.get_status_summary()
        status_icon = "🟢 ACTIVE" if summary["bot_enabled"] else "🔴 PAUSED"
        conn_icon = "✅ CONNECTED" if summary["connected"] else "❌ DISCONNECTED"

        msg = (
            f"📊 <b>System & Trading Status</b>\n\n"
            f"<b>Bot Status:</b> {status_icon}\n"
            f"<b>MT5 Broker:</b> {conn_icon} (Server: <code>{summary['server']}</code>)\n"
            f"<b>Account Login:</b> <code>{summary['login']}</code>\n\n"
            f"💰 <b>Balance:</b> {summary['balance']} {summary['currency']}\n"
            f"📈 <b>Equity:</b> {summary['equity']} {summary['currency']}\n"
            f"🔒 <b>Margin:</b> {summary['margin']} | <b>Free:</b> {summary['free_margin']}\n"
            f"📂 <b>Open Positions:</b> {summary['open_positions_count']}"
        )
        keyboard = build_status_keyboard(summary["bot_enabled"])
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    @admin_only
    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        positions = await self.service.broker.get_positions()
        if not positions:
            await update.message.reply_text("📂 <b>No open positions.</b>", parse_mode=ParseMode.HTML)
            return

        lines = ["📂 <b>Active Positions:</b>\n"]
        for p in positions:
            pnl_icon = "🟢" if p.profit >= 0 else "🔴"
            lines.append(
                f"• Ticket <code>{p.ticket}</code>: {p.position_type} {p.volume} <b>{p.symbol}</b> @ {p.open_price}\n"
                f"  Curr: {p.current_price} | PnL: {pnl_icon} <b>${p.profit:.2f}</b> | SL: {p.sl or 'None'} | TP: {p.tp or 'None'}"
            )

        keyboard = build_positions_keyboard(positions)
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=keyboard)

    @admin_only
    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        acc = await self.service.broker.get_account_info()
        msg = (
            f"💰 <b>Account Financial Overview</b>\n\n"
            f"<b>Balance:</b> {acc.balance} {acc.currency}\n"
            f"<b>Equity:</b> {acc.equity} {acc.currency}\n"
            f"<b>Margin Used:</b> {acc.margin}\n"
            f"<b>Free Margin:</b> {acc.free_margin}\n"
            f"<b>Leverage:</b> 1:{acc.leverage}\n"
            f"<b>Server:</b> {acc.server}"
        )
        if update.effective_message:
            await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)

    async def _format_history_message(self, days: int = 7) -> tuple[str, InlineKeyboardMarkup]:
        deals = await self.service.get_closed_deals(days=days, limit=15)
        if not deals:
            msg = (
                f"📜 <b>Lịch Sử Lệnh Đã Đóng</b>\n"
                f"⏱️ <i>Kỳ xem: {days} ngày gần nhất</i>\n\n"
                f"ℹ️ Không có lệnh nào đã đóng trong khoảng thời gian này."
            )
        else:
            lines = [f"📜 <b>Lịch Sử Lệnh Đã Đóng ({days} ngày qua):</b>\n"]
            for d in deals:
                pnl_icon = "🟢" if d.profit > Decimal("0") else ("🔴" if d.profit < Decimal("0") else "⚪")
                pnl_sign = "+" if d.profit > Decimal("0") else ""
                net = d.profit + d.swap + d.commission
                time_str = d.time.strftime("%d/%m %H:%M")
                lines.append(
                    f"• {pnl_icon} <b>{d.symbol}</b> {d.deal_type} {d.volume} @ {d.price}\n"
                    f"  PnL: <b>{pnl_sign}${d.profit:.2f}</b> (Net: ${net:.2f}) | {time_str} | #{d.ticket}"
                )
            lines.append(f"\n📊 <i>Hiển thị {len(deals)} lệnh gần nhất.</i>")
            msg = "\n".join(lines)

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
            f"📈 <b>Báo Cáo Hiệu Suất & Thống Kê Win/Loss</b>\n"
            f"⏱️ <i>Kỳ thống kê: {days} ngày gần nhất</i>\n"
            f"────────────────────────\n"
            f"🎯 <b>Tổng lệnh đã đóng:</b> {stats['total_trades']}\n"
            f"✅ <b>Lệnh Thắng (Win):</b> {stats['win_trades']} (<b>{stats['win_rate']}%</b>)\n"
            f"❌ <b>Lệnh Thua (Loss):</b> {stats['loss_trades']}\n"
            f"⚪ <b>Hòa vốn (BE):</b> {stats['breakeven_trades']}\n"
            f"────────────────────────\n"
            f"💵 <b>Tổng Lợi Nhuận:</b> +${stats['total_profit']:.2f}\n"
            f"💸 <b>Tổng Thua Lỗ:</b> -${abs(stats['total_loss']):.2f}\n"
            f"⚖️ <b>Profit Factor:</b> <code>{pf_str}</code>\n"
            f"💰 <b>Lãi Ròng (Net PnL):</b> {pnl_icon} <b>{pnl_sign}${net_pnl:.2f}</b>\n"
            f"────────────────────────\n"
            f"🏆 <b>Lệnh thắng lớn nhất:</b> +${stats['best_trade']:.2f}\n"
            f"💥 <b>Lệnh thua lớn nhất:</b> ${stats['worst_trade']:.2f}"
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
            await update.message.reply_text("Usage: <code>/close &lt;ticket&gt;</code>", parse_mode=ParseMode.HTML)
            return

        try:
            ticket = int(context.args[0])
            res = await self.service.close_position_by_ticket(ticket)
            if res.success:
                await update.message.reply_text(f"✅ Closed position <code>{ticket}</code> successfully!", parse_mode=ParseMode.HTML)
            else:
                await update.message.reply_text(f"❌ Failed to close <code>{ticket}</code>: {res.comment}", parse_mode=ParseMode.HTML)
        except ValueError:
            await update.message.reply_text("❌ Ticket must be an integer.")

    @admin_only
    async def cmd_closeall(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        results = await self.service.close_all_positions()
        if not results:
            await update.message.reply_text("No positions were open.")
            return

        successes = sum(1 for r in results if r.success)
        await update.message.reply_text(
            f"🚨 <b>Close All Completed</b>\nClosed {successes}/{len(results)} positions.",
            parse_mode=ParseMode.HTML,
        )

    @admin_only
    async def cmd_partial(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: <code>/partial &lt;ticket&gt; &lt;percent&gt;</code>\nExample: <code>/partial 123456 50</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        try:
            ticket = int(args[0])
            percent = Decimal(args[1])
            positions = await self.service.broker.get_positions()
            target = next((p for p in positions if p.ticket == ticket), None)
            if not target:
                await update.message.reply_text(f"❌ Position {ticket} not found.")
                return

            vol = (target.volume * (percent / Decimal("100"))).quantize(Decimal("0.01"))
            vol = max(Decimal("0.01"), min(vol, target.volume))

            res = await self.service.close_position_by_ticket(ticket, volume=vol)
            if res.success:
                await update.message.reply_text(f"✅ Partial closed {vol} lot on ticket <code>{ticket}</code>!", parse_mode=ParseMode.HTML)
            else:
                await update.message.reply_text(f"❌ Failed: {res.comment}")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

    @admin_only
    async def cmd_setsl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text("Usage: <code>/setsl &lt;ticket&gt; &lt;price&gt;</code>", parse_mode=ParseMode.HTML)
            return
        try:
            ticket = int(args[0])
            price = Decimal(args[1])
            res = await self.service.modify_sl_tp(ticket, sl=price)
            if res.success:
                await update.message.reply_text(f"✅ Stop Loss on <code>{ticket}</code> set to {price}!", parse_mode=ParseMode.HTML)
            else:
                await update.message.reply_text(f"❌ Failed: {res.comment}")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

    @admin_only
    async def cmd_settp(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text("Usage: <code>/settp &lt;ticket&gt; &lt;price&gt;</code>", parse_mode=ParseMode.HTML)
            return
        try:
            ticket = int(args[0])
            price = Decimal(args[1])
            res = await self.service.modify_sl_tp(ticket, tp=price)
            if res.success:
                await update.message.reply_text(f"✅ Take Profit on <code>{ticket}</code> set to {price}!", parse_mode=ParseMode.HTML)
            else:
                await update.message.reply_text(f"❌ Failed: {res.comment}")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

    @admin_only
    async def cmd_breakeven(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.message.reply_text("Usage: <code>/breakeven &lt;ticket&gt;</code>", parse_mode=ParseMode.HTML)
            return
        try:
            ticket = int(context.args[0])
            res = await self.service.move_to_breakeven(ticket)
            if res.success:
                await update.message.reply_text(f"🎯 Moved SL on <code>{ticket}</code> to Entry price!", parse_mode=ParseMode.HTML)
            else:
                await update.message.reply_text(f"❌ Failed: {res.comment}")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")

    @admin_only
    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.service.risk_config.enabled = False
        await update.message.reply_text("⏸️ <b>Bot PAUSED.</b> Signal execution is suspended.", parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.service.risk_config.enabled = True
        await update.message.reply_text("▶️ <b>Bot RESUMED.</b> Normal signal processing active.", parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        connected = await self.service.broker.is_connected()
        status_broker = "✅ OK" if connected else "❌ Disconnected"
        msg = (
            "🏥 <b>Health Status:</b>\n\n"
            f"• <b>MT5 Connection:</b> {status_broker}\n"
            f"• <b>Signal Listener:</b> ✅ Ready\n"
            f"• <b>Risk Engine:</b> ✅ Active\n"
            f"• <b>Allowed Symbols:</b> {', '.join(self.service.risk_config.allowed_symbols)}"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_kill(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.service.risk_config.enabled = False
        results = await self.service.close_all_positions()
        await update.message.reply_text(
            f"🚨 <b>EMERGENCY KILL SWITCH ACTIVATED</b>\n"
            f"• Bot disabled.\n"
            f"• Closed {len(results)} active positions.",
            parse_mode=ParseMode.HTML,
        )

    @admin_only
    async def cmd_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        cfg = self.service.risk_config

        if args and args[0].lower() == "set" and len(args) >= 3:
            key = args[1]
            val = " ".join(args[2:])
            if cfg.update_field(key, val):
                await update.message.reply_text(f"✅ Updated <code>{key}</code> = <b>{getattr(cfg, key)}</b>", parse_mode=ParseMode.HTML)
            else:
                await update.message.reply_text(f"❌ Unknown config key: <code>{key}</code>", parse_mode=ParseMode.HTML)
            return

        msg = (
            "⚙️ <b>Live Risk & Management Configuration</b>\n\n"
            f"• <b>Enabled:</b> {cfg.enabled}\n"
            f"• <b>Dual Entry Setup:</b> {'ON' if cfg.enable_dual_entry else 'OFF'}\n"
            f"• <b>Lot Min Entry:</b> {cfg.lot_min_entry}\n"
            f"• <b>Lot Max Entry:</b> {cfg.lot_max_entry}\n"
            f"• <b>Dung sai trượt giá:</b> {cfg.max_slippage_entry} giá\n"
            f"• <b>Max Open Positions:</b> {cfg.max_open_positions}\n"
            f"• <b>Default Lot:</b> {cfg.default_lot}\n"
            f"• <b>Max Lot:</b> {cfg.max_lot}\n"
            f"• <b>Min Lot:</b> {cfg.min_lot}\n"
            f"• <b>Max Daily Loss:</b> {cfg.max_daily_loss_percent}%\n"
            f"• <b>Require Stop Loss:</b> {cfg.require_sl}\n"
            f"• <b>Allowed Symbols:</b> {', '.join(cfg.allowed_symbols)}\n"
            f"• <b>Default Partial Close:</b> {cfg.default_partial_close_percent}%\n"
            f"• <b>Auto Breakeven on Partial:</b> {cfg.auto_breakeven_on_partial}\n"
            f"• <b>Signal Timeout:</b> {cfg.signal_expire_seconds}s\n\n"
            "<i>Sử dụng nút bấm bên dưới hoặc câu lệnh:</i>\n"
            "<code>/config set lot_min_entry 0.02</code>\n"
            "<code>/config set lot_max_entry 0.01</code>\n"
            "<code>/config set max_slippage_entry 1.5</code>\n"
            "<code>/config set enable_dual_entry true</code>"
        )
        keyboard = build_risk_config_keyboard(cfg)
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    @admin_only
    async def cmd_rules(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Display or reload Vietnamese keyword management rules."""
        args = context.args or []
        if args and args[0].lower() == "reload":
            count = await self.service.load_management_rules_from_db()
            await update.message.reply_text(
                f"🔄 Đã tải lại <b>{count}</b> quy tắc từ PostgreSQL vào hệ thống!",
                parse_mode=ParseMode.HTML,
            )
            return

        rules = self.service.get_management_rules()
        lines = [
            "📖 <b>Từ khóa quản lý lệnh (Vietnamese Rules):</b>\n",
            "<i>Bot nhận diện các từ khóa này trong tin nhắn Telegram để thực thi quản lý vị thế:</i>\n",
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
            "\n💡 <b>Lệnh tinh chỉnh trực tiếp:</b>",
            "• Thêm/sửa: <code>/rule add &lt;từ khóa&gt; &lt;ACTION&gt; [params]</code>",
            "  <i>VD:</i> <code>/rule add \"cắt bớt\" PARTIAL_CLOSE 50</code>",
            "  <i>VD:</i> <code>/rule add \"về hòa\" BREAKEVEN</code>",
            "  <i>VD:</i> <code>/rule add \"chốt sạch\" CLOSE</code>",
            "• Xóa từ khóa: <code>/rule del &lt;từ khóa&gt;</code>",
            "  <i>VD:</i> <code>/rule del \"chốt sạch\"</code>",
            "• Nạp lại từ DB: <code>/rules reload</code>",
            "\n<i>Hỗ trợ các ACTION: PARTIAL_CLOSE, CLOSE, MOVE_SL, BREAKEVEN, REENTRY</i>",
        ])

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

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
                "Usage:\n"
                "• <code>/rule add &lt;từ khóa&gt; &lt;ACTION&gt; [params]</code>\n"
                "• <code>/rule del &lt;từ khóa&gt;</code>\n\n"
                "<i>Xem danh sách: /rules</i>",
                parse_mode=ParseMode.HTML,
            )
            return

        subcmd = tokens[1].lower()

        if subcmd == "add":
            if len(tokens) < 4:
                await update.message.reply_text(
                    "❌ Cú pháp: <code>/rule add &lt;keyword&gt; &lt;ACTION&gt; [params]</code>\n"
                    "VD: <code>/rule add \"chốt 1/2\" PARTIAL_CLOSE 50</code>\n"
                    "ACTIONS: PARTIAL_CLOSE, CLOSE, MOVE_SL, BREAKEVEN, REENTRY",
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
                    f"✅ Đã lưu từ khóa quản lý lệnh:\n"
                    f"• Từ khóa: <code>{keyword}</code>\n"
                    f"• Hành động: <b>{action_type_str}</b>\n"
                    f"• Tham số: {percentage or target or 'Mặc định'}\n\n"
                    f"<i>Đã cập nhật hệ thống và lưu trữ vào PostgreSQL.</i>",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await update.message.reply_text(
                    f"❌ Lỗi: Action '<code>{action_type_str}</code>' không hợp lệ.\n"
                    f"Các action được hỗ trợ: PARTIAL_CLOSE, CLOSE, MOVE_SL, BREAKEVEN, REENTRY",
                    parse_mode=ParseMode.HTML,
                )

        elif subcmd in ("del", "delete", "remove"):
            if len(tokens) < 3:
                await update.message.reply_text("❌ Thiếu từ khóa cần xóa. VD: <code>/rule del \"chốt\"</code>", parse_mode=ParseMode.HTML)
                return
            keyword = tokens[2]
            ok = await self.service.remove_management_rule(keyword)
            if ok:
                await update.message.reply_text(
                    f"🗑️ Đã xóa từ khóa <code>{keyword}</code> khỏi hệ thống và database.",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await update.message.reply_text(
                    f"⚠️ Không tìm thấy từ khóa <code>{keyword}</code> trong danh sách đang hoạt động.",
                    parse_mode=ParseMode.HTML,
                )
        else:
            await update.message.reply_text("❌ Lệnh phụ không hợp lệ. Dùng <code>add</code> hoặc <code>del</code>.", parse_mode=ParseMode.HTML)


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
            await query.edit_message_reply_markup(reply_markup=build_status_keyboard(False))
        elif data == "bot_resume":
            cfg.enabled = True
            await query.edit_message_reply_markup(reply_markup=build_status_keyboard(True))
        elif data == "toggle_bot_enabled":
            cfg.enabled = not cfg.enabled
            await query.edit_message_reply_markup(reply_markup=build_risk_config_keyboard(cfg))
        elif data == "toggle_require_sl":
            cfg.require_sl = not cfg.require_sl
            await query.edit_message_reply_markup(reply_markup=build_risk_config_keyboard(cfg))
        elif data == "toggle_auto_be":
            cfg.auto_breakeven_on_partial = not cfg.auto_breakeven_on_partial
            await query.edit_message_reply_markup(reply_markup=build_risk_config_keyboard(cfg))
        elif data == "toggle_dual_entry":
            cfg.enable_dual_entry = not cfg.enable_dual_entry
            await query.edit_message_reply_markup(reply_markup=build_risk_config_keyboard(cfg))
        elif data == "cycle_max_slippage":
            slips = [Decimal("0.5"), Decimal("1.0"), Decimal("1.5"), Decimal("2.0"), Decimal("3.0")]
            try:
                curr_idx = slips.index(cfg.max_slippage_entry)
                cfg.max_slippage_entry = slips[(curr_idx + 1) % len(slips)]
            except ValueError:
                cfg.max_slippage_entry = Decimal("1.0")
            await query.edit_message_reply_markup(reply_markup=build_risk_config_keyboard(cfg))
        elif data == "cycle_lot_min_entry":
            lots = [Decimal("0.01"), Decimal("0.02"), Decimal("0.03"), Decimal("0.05"), Decimal("0.10")]
            try:
                curr_idx = lots.index(cfg.lot_min_entry)
                cfg.lot_min_entry = lots[(curr_idx + 1) % len(lots)]
            except ValueError:
                cfg.lot_min_entry = Decimal("0.01")
            await query.edit_message_reply_markup(reply_markup=build_risk_config_keyboard(cfg))
        elif data == "cycle_lot_max_entry":
            lots = [Decimal("0.01"), Decimal("0.02"), Decimal("0.03"), Decimal("0.05"), Decimal("0.10")]
            try:
                curr_idx = lots.index(cfg.lot_max_entry)
                cfg.lot_max_entry = lots[(curr_idx + 1) % len(lots)]
            except ValueError:
                cfg.lot_max_entry = Decimal("0.01")
            await query.edit_message_reply_markup(reply_markup=build_risk_config_keyboard(cfg))
        elif data == "edit_max_pos":
            positions_list = [1, 2, 3, 5, 10]
            try:
                curr_idx = positions_list.index(cfg.max_open_positions)
                cfg.max_open_positions = positions_list[(curr_idx + 1) % len(positions_list)]
            except ValueError:
                cfg.max_open_positions = 5
            await query.edit_message_reply_markup(reply_markup=build_risk_config_keyboard(cfg))
        elif data == "toggle_partial_pct":
            pcts = [Decimal("30.0"), Decimal("50.0"), Decimal("70.0")]
            try:
                curr_idx = pcts.index(cfg.default_partial_close_percent)
                cfg.default_partial_close_percent = pcts[(curr_idx + 1) % len(pcts)]
            except ValueError:
                cfg.default_partial_close_percent = Decimal("50.0")
            await query.edit_message_reply_markup(reply_markup=build_risk_config_keyboard(cfg))
        elif data.startswith("close_"):
            ticket_str = data.split("_")[1]
            if ticket_str == "all":
                await self.service.close_all_positions()
                await query.edit_message_text("🚨 All positions closed.")
            else:
                try:
                    ticket = int(ticket_str)
                    res = await self.service.close_position_by_ticket(ticket)
                    if res.success:
                        await query.edit_message_text(f"✅ Closed ticket {ticket}.")
                    else:
                        await query.edit_message_text(f"❌ Failed to close {ticket}: {res.comment}")
                except ValueError:
                    pass
        elif data.startswith("be_"):
            ticket = int(data.split("_")[1])
            res = await self.service.move_to_breakeven(ticket)
            if res.success:
                await query.edit_message_text(f"🎯 Moved SL on ticket {ticket} to Entry!")
            else:
                await query.edit_message_text(f"❌ Failed to move SL: {res.comment}")
        elif data in ("view_positions", "positions_refresh"):
            positions = await self.service.broker.get_positions()
            if not positions:
                keyboard = build_status_keyboard(cfg.enabled)
                await query.edit_message_text("📂 <b>No open positions.</b>", parse_mode=ParseMode.HTML, reply_markup=keyboard)
            else:
                lines = ["📂 <b>Active Positions:</b>\n"]
                for p in positions:
                    pnl_icon = "🟢" if p.profit >= Decimal("0") else "🔴"
                    lines.append(
                        f"• Ticket <code>{p.ticket}</code>: {p.position_type} {p.volume} <b>{p.symbol}</b> @ {p.open_price}\n"
                        f"  Curr: {p.current_price} | PnL: {pnl_icon} <b>${p.profit:.2f}</b> | SL: {p.sl or 'None'} | TP: {p.tp or 'None'}"
                    )
                keyboard = build_positions_keyboard(positions)
                await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=keyboard)
        elif data == "view_orders":
            from app.telegram.keyboards import build_pending_orders_keyboard
            orders = await self.service.broker.get_pending_orders()
            if not orders:
                keyboard = build_status_keyboard(cfg.enabled)
                await query.edit_message_text("⏳ <b>Không có lệnh chờ (Pending Orders).</b>", parse_mode=ParseMode.HTML, reply_markup=keyboard)
            else:
                lines = ["⏳ <b>Lệnh chờ hiện tại (Pending Orders):</b>\n"]
                for o in orders:
                    lines.append(
                        f"• Ticket <code>{o.ticket}</code>: {o.order_type} {o.volume} <b>{o.symbol}</b> @ {o.price}\n"
                        f"  SL: {o.sl or 'None'} | TP: {o.tp or 'None'}"
                    )
                keyboard = build_pending_orders_keyboard(orders)
                await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=keyboard)
        elif data.startswith("cancel_") and data != "cancel_all_orders":
            ticket = int(data.split("_")[1])
            res = await self.service.broker.cancel_order(ticket)
            if res.success:
                await query.edit_message_text(f"✅ Đã hủy lệnh chờ {ticket}.")
            else:
                await query.edit_message_text(f"❌ Không thể hủy lệnh chờ {ticket}: {res.comment}")
        elif data == "cancel_all_orders":
            orders = await self.service.broker.get_pending_orders()
            results = []
            for o in orders:
                res = await self.service.broker.cancel_order(o.ticket)
                if res.success:
                    results.append(f"✅ Hủy thành công: {o.ticket}")
                else:
                    results.append(f"❌ Lỗi hủy {o.ticket}: {res.comment}")
            msg = "\n".join(results) if results else "Không có lệnh chờ nào."
            await query.edit_message_text(msg)
        elif data == "view_history":
            msg, keyboard = await self._format_history_message(7)
            await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        elif data.startswith("override_"):
            parts = data.split("_")
            action = parts[1]  # min, max, ignore
            try:
                sig_id = int(parts[2])
                if action == "ignore":
                    # Just remove the keyboard and add a note
                    await query.edit_message_text(
                        f"{query.message.text_html}\n\n❌ <i>Đã bỏ qua lệnh chờ limit.</i>", 
                        parse_mode=ParseMode.HTML
                    )
                else:
                    res_msg = await self.service.execute_override_order(sig_id, action)
                    # Append result to the message and remove keyboard
                    await query.edit_message_text(
                        f"{query.message.text_html}\n\n{res_msg}",
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
            status_icon = "🟢 ACTIVE" if summary["bot_enabled"] else "🔴 PAUSED"
            conn_icon = "✅ CONNECTED" if summary["connected"] else "❌ DISCONNECTED"
            msg = (
                f"📊 <b>System & Trading Status</b>\n\n"
                f"<b>Bot Status:</b> {status_icon}\n"
                f"<b>MT5 Broker:</b> {conn_icon} (Server: <code>{summary['server']}</code>)\n"
                f"<b>Account Login:</b> <code>{summary['login']}</code>\n\n"
                f"💰 <b>Balance:</b> {summary['balance']} {summary['currency']}\n"
                f"📈 <b>Equity:</b> {summary['equity']} {summary['currency']}\n"
                f"🔒 <b>Margin:</b> {summary['margin']} | <b>Free:</b> {summary['free_margin']}\n"
                f"📂 <b>Open Positions:</b> {summary['open_positions_count']}\n"
                f"⏳ <b>Pending Orders:</b> {summary.get('pending_orders_count', 0)}"
            )
            keyboard = build_status_keyboard(summary["bot_enabled"])
            try:
                await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise


# Singleton Personal Bot instance
personal_bot = PersonalTelegramBot()
