"""
Inline keyboards for Telegram Personal Bot Admin Console.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.broker.base import PositionInfo
from app.trading.risk import RiskConfig


def build_status_keyboard(bot_enabled: bool) -> InlineKeyboardMarkup:
    """Build interactive action buttons for /status."""
    toggle_text = "⏸️ Pause Bot" if bot_enabled else "▶️ Resume Bot"
    toggle_data = "bot_pause" if bot_enabled else "bot_resume"

    keyboard = [
        [
            InlineKeyboardButton(toggle_text, callback_data=toggle_data),
            InlineKeyboardButton("🔄 Refresh", callback_data="status_refresh"),
        ],
        [
            InlineKeyboardButton("📋 Vị Thế (Positions)", callback_data="view_positions"),
            InlineKeyboardButton("⏳ Lệnh Chờ (Orders)", callback_data="view_orders"),
        ],
        [
            InlineKeyboardButton("⚙️ Config Risk", callback_data="view_config_risk"),
        ],
        [
            InlineKeyboardButton("📜 Lịch sử lệnh", callback_data="view_history"),
            InlineKeyboardButton("📈 Thống kê Win/Loss", callback_data="view_stats"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_positions_keyboard(positions: list[PositionInfo]) -> InlineKeyboardMarkup:
    """Build action buttons for each active position (Close / Breakeven / Partial)."""
    buttons = []
    for pos in positions[:6]:  # Max 6 on screen
        row = [
            InlineKeyboardButton(f"❌ Đóng {pos.ticket}", callback_data=f"close_{pos.ticket}"),
            InlineKeyboardButton(f"✂️ Chốt 50%", callback_data=f"partial_{pos.ticket}_50"),
            InlineKeyboardButton(f"🎯 BE", callback_data=f"be_{pos.ticket}"),
        ]
        buttons.append(row)

    if positions:
        buttons.append([
            InlineKeyboardButton("✂️ Chốt 50% Tất Cả", callback_data="partial_all_50"),
            InlineKeyboardButton("🚨 Đóng Toàn Bộ", callback_data="close_all_confirm"),
        ])
    buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data="positions_refresh")])

    return InlineKeyboardMarkup(buttons)


def build_risk_config_keyboard(config: RiskConfig) -> InlineKeyboardMarkup:
    """Build interactive toggle & setting buttons for /config risk."""
    sl_toggle = "✅ Require SL: ON" if config.require_sl else "❌ Require SL: OFF"
    bot_toggle = "🟢 Bot: ACTIVE" if config.enabled else "🔴 Bot: PAUSED"
    dual_toggle = "⚡ Dual Entry: ON" if config.enable_dual_entry else "⚡ Dual Entry: OFF"

    keyboard = [
        [
            InlineKeyboardButton(bot_toggle, callback_data="toggle_bot_enabled"),
            InlineKeyboardButton(sl_toggle, callback_data="toggle_require_sl"),
        ],
        [
            InlineKeyboardButton(dual_toggle, callback_data="toggle_dual_entry"),
            InlineKeyboardButton(f"Dung sai: {config.max_slippage_entry} giá", callback_data="cycle_max_slippage"),
        ],
        [
            InlineKeyboardButton(f"Min Lot: {config.lot_min_entry}", callback_data="cycle_lot_min_entry"),
            InlineKeyboardButton(f"Max Lot: {config.lot_max_entry}", callback_data="cycle_lot_max_entry"),
        ],
        [
            InlineKeyboardButton(f"Max Pos: {config.max_open_positions}", callback_data="edit_max_pos"),
            InlineKeyboardButton(f"Def Lot: {config.default_lot}", callback_data="edit_def_lot"),
        ],
        [
            InlineKeyboardButton(f"Partial: {config.default_partial_close_percent}%", callback_data="toggle_partial_pct"),
            InlineKeyboardButton(f"Auto BE: {'ON' if config.auto_breakeven_on_partial else 'OFF'}", callback_data="toggle_auto_be"),
        ],
        [
            InlineKeyboardButton("🔙 Back to Status", callback_data="status_refresh"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_history_keyboard(days: int = 7) -> InlineKeyboardMarkup:
    """Build interactive time filter buttons for trade history."""
    buttons = [
        [
            InlineKeyboardButton("📅 Hôm Nay", callback_data="history_1"),
            InlineKeyboardButton("📅 7 Ngày", callback_data="history_7"),
            InlineKeyboardButton("📅 30 Ngày", callback_data="history_30"),
        ],
        [
            InlineKeyboardButton("📈 Thống Kê Win/Loss", callback_data="view_stats"),
            InlineKeyboardButton("🔄 Nạp lại", callback_data=f"history_{days}"),
        ],
        [
            InlineKeyboardButton("🔙 Quay lại Status", callback_data="status_refresh"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def build_stats_keyboard(days: int = 7) -> InlineKeyboardMarkup:
    """Build interactive time filter buttons for Win/Loss performance statistics."""
    buttons = [
        [
            InlineKeyboardButton("📅 Hôm Nay", callback_data="stats_1"),
            InlineKeyboardButton("📅 7 Ngày", callback_data="stats_7"),
            InlineKeyboardButton("📅 30 Ngày", callback_data="stats_30"),
        ],
        [
            InlineKeyboardButton("📜 Xem Lịch Sử Lệnh", callback_data=f"history_{days}"),
            InlineKeyboardButton("🔄 Nạp lại", callback_data=f"stats_{days}"),
        ],
        [
            InlineKeyboardButton("🔙 Quay lại Status", callback_data="status_refresh"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def build_override_keyboard(signal_id: int, has_dual: bool = True) -> InlineKeyboardMarkup:
    """Build buttons to override rejected signals and place orders."""
    confirm_label = "✅ Vẫn vào lệnh (Cả 2 lệnh)" if has_dual else "✅ Vẫn vào lệnh (Override)"
    keyboard = [
        [InlineKeyboardButton(confirm_label, callback_data=f"override_both_{signal_id}")],
        [InlineKeyboardButton("❌ Bỏ qua", callback_data=f"override_ignore_{signal_id}")],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_pending_orders_keyboard(orders: list[Any]) -> InlineKeyboardMarkup:
    """Build keyboard for managing pending orders."""
    keyboard = []
    # Add a cancel button for each pending order
    for o in orders:
        keyboard.append([InlineKeyboardButton(f"❌ Cancel #{o.ticket} ({o.symbol})", callback_data=f"cancel_{o.ticket}")])
        
    if orders:
        keyboard.append([InlineKeyboardButton("❌ Hủy Tất Cả Pending Orders", callback_data="cancel_all_orders")])
        
    keyboard.append([
        InlineKeyboardButton("🔄 Nạp lại", callback_data="view_orders"),
        InlineKeyboardButton("🔙 Quay lại Status", callback_data="status_refresh")
    ])
    return InlineKeyboardMarkup(keyboard)

