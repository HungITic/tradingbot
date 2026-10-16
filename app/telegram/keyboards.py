"""
Inline keyboards for Telegram Personal Bot Admin Console.
"""

from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.broker.base import PositionInfo
from app.trading.risk import RiskConfig
from app.telegram.configuration import CONFIG_FIELDS, CONFIG_SECTIONS


def build_status_keyboard(bot_enabled: bool) -> InlineKeyboardMarkup:
    """Build interactive action buttons for /status."""
    toggle_text = "⏸️ Dừng mở lệnh mới" if bot_enabled else "▶️ Cho phép mở lệnh mới"
    toggle_data = "bot_pause" if bot_enabled else "bot_resume"

    keyboard = [
        [InlineKeyboardButton("📥 Tín hiệu", callback_data="inbox_ALL_0"),
         InlineKeyboardButton("🗂 Kèo đang chạy", callback_data="setups_0")],
        [
            InlineKeyboardButton(toggle_text, callback_data=toggle_data),
            InlineKeyboardButton("🔄 Cập nhật", callback_data="status_refresh"),
        ],
        [
            InlineKeyboardButton("📋 Vị thế", callback_data="view_positions"),
            InlineKeyboardButton("⏳ Lệnh chờ", callback_data="view_orders"),
        ],
        [
            InlineKeyboardButton("⚙️ Cài đặt", callback_data="view_config_risk"),
        ],
        [
            InlineKeyboardButton("📜 Lịch sử lệnh", callback_data="view_history"),
            InlineKeyboardButton("📈 Thống kê Win/Loss", callback_data="view_stats"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_positions_keyboard(positions: list[PositionInfo], page: int = 0, percentage=50) -> InlineKeyboardMarkup:
    """Build action buttons for each active position (Close / Breakeven / Partial)."""
    buttons = []
    for index, pos in enumerate(positions[page * 5:page * 5 + 5], start=page * 5 + 1):
        row = [
            InlineKeyboardButton(f"{index}. Đóng #{pos.ticket}", callback_data=f"close_{pos.ticket}"),
            InlineKeyboardButton(f"{index}. Chốt {percentage}%", callback_data=f"partial_{pos.ticket}_{percentage}"),
            InlineKeyboardButton(f"{index}. Hòa vốn", callback_data=f"be_{pos.ticket}"),
        ]
        buttons.append(row)

    if positions:
        buttons.append([
            InlineKeyboardButton(f"✂️ Chốt {percentage}% tất cả", callback_data=f"partial_all_{percentage}"),
            InlineKeyboardButton("🚨 Đóng Toàn Bộ", callback_data="close_all_confirm"),
        ])
    nav = []
    if page:
        nav.append(InlineKeyboardButton("← Trước", callback_data=f"positions_page_{page - 1}"))
    if len(positions) > (page + 1) * 5:
        nav.append(InlineKeyboardButton("Sau →", callback_data=f"positions_page_{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔄 Cập nhật", callback_data=f"positions_page_{page}"),
                    InlineKeyboardButton("Tổng quan", callback_data="status_refresh")])

    return InlineKeyboardMarkup(buttons)




def build_risk_config_keyboard(config: RiskConfig, group: str | None = None) -> InlineKeyboardMarkup:
    """Build interactive toggle & setting buttons for /config risk."""
    if group not in CONFIG_SECTIONS:
        return InlineKeyboardMarkup([
            *[[InlineKeyboardButton(label, callback_data=f"cfg_group_{key}")] for key, (label, _) in CONFIG_SECTIONS.items()],
            [InlineKeyboardButton("Tổng quan", callback_data="status_refresh")],
        ])
    buttons = []
    for key, label in CONFIG_FIELDS.items():
        if key not in CONFIG_SECTIONS[group][1]:
            continue
        value = getattr(config, key)
        if isinstance(value, bool):
            value = "Bật" if value else "Tắt"
        elif key == "tp1_min_volume_action":
            value = "Dời SL" if value == "breakeven" else "Bỏ qua"
        elif isinstance(value, list):
            value = ", ".join(value)
        buttons.append(InlineKeyboardButton(f"{label}: {value}", callback_data=f"cfg_{key}"))
    rows = [[button] for button in buttons]
    rows.append([InlineKeyboardButton("← Nhóm cài đặt", callback_data="view_config_risk")])
    return InlineKeyboardMarkup(rows)


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
            InlineKeyboardButton("🔙 Tổng quan", callback_data="status_refresh"),
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
            InlineKeyboardButton("🔙 Tổng quan", callback_data="status_refresh"),
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


def build_pending_orders_keyboard(orders: list[Any], page: int = 0) -> InlineKeyboardMarkup:
    """Build keyboard for managing pending orders."""
    keyboard = []
    # Add a cancel button for each pending order
    for o in orders[page * 5:page * 5 + 5]:
        keyboard.append([InlineKeyboardButton(f"❌ Hủy #{o.ticket} ({o.symbol})", callback_data=f"cancel_{o.ticket}")])
        
    if orders:
        keyboard.append([InlineKeyboardButton("❌ Hủy tất cả lệnh chờ", callback_data="cancel_all_orders")])
        
    nav = []
    if page:
        nav.append(InlineKeyboardButton("← Trước", callback_data=f"orders_page_{page - 1}"))
    if len(orders) > (page + 1) * 5:
        nav.append(InlineKeyboardButton("Sau →", callback_data=f"orders_page_{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([
        InlineKeyboardButton("🔄 Cập nhật", callback_data=f"orders_page_{page}"),
        InlineKeyboardButton("🔙 Tổng quan", callback_data="status_refresh")
    ])
    return InlineKeyboardMarkup(keyboard)

