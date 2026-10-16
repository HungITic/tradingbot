"""Exit reports use broker reason codes and exact execution IDs, never PnL/price guesses."""
from __future__ import annotations

from html import escape

import structlog
from sqlalchemy import or_, select

from app.broker.mt5.constants import (
    DEAL_REASON_SL, DEAL_REASON_TP, DEAL_REASON_SO,
)
from app.config import database
from app.core.timezone import format_vietnam_time
from app.models.trade import ManagementAction
from app.telegram.presentation import heading, section, field, money, price, number, join_cards

logger = structlog.get_logger(__name__)

BROKER_REASONS = {
    0: "Đóng từ MT5 trên máy tính",
    1: "Đóng từ ứng dụng di động",
    2: "Đóng từ nền tảng web",
    3: "Đóng bởi chương trình giao dịch (MT5)",
    DEAL_REASON_SL: "Khớp Stop Loss (SL)",
    DEAL_REASON_TP: "Đạt Take Profit (TP)",
    DEAL_REASON_SO: "Stop Out · Sàn đóng do thiếu ký quỹ",
    7: "Rollover",
    8: "Điều chỉnh ký quỹ",
    9: "Điều chỉnh do chia tách",
    10: "Điều chỉnh doanh nghiệp",
}
CLOSE_CAUSES = {
    "TP1_PARTIAL": "Chốt một phần tại TP1",
    "FINAL_TP": "Bot đóng tại TP cuối",
    "MIN_ENTRY_PARTIAL": "Tín hiệu chốt bớt entry xấu · Min Entry",
    "SIGNAL_PARTIAL": "Chốt một phần theo tín hiệu Telegram",
    "SIGNAL_CLOSE": "Đóng lệnh theo tín hiệu Telegram",
    "MANUAL_PARTIAL": "Chốt một phần từ menu / command Telegram",
    "MANUAL_CLOSE": "Đóng lệnh từ menu / command Telegram",
    "CLOSE_ALL": "Đóng toàn bộ từ Telegram",
    "KILL_SWITCH": "Dừng khẩn cấp từ Telegram",
}


class CloseReportingMixin:
    def close_execution_metadata(self, position_ticket, result, cause, *, partial=False):
        """Persist only execution IDs actually returned by the broker, not position IDs as order IDs."""
        raw = result.raw or {}
        metadata = {"close_cause": cause, "partial_close": partial}
        for raw_key, key in [("deal", "broker_deal_id"), ("order", "broker_order_id")]:
            value = raw.get(raw_key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                metadata[key] = value
        if result.success:
            for key in ("broker_deal_id", "broker_order_id"):
                if key in metadata:
                    self._close_executions[(key, metadata[key])] = {**metadata, "ticket": position_ticket}
        return metadata

    async def exit_context(self, deal):
        for key, value in [("broker_deal_id", deal.ticket), ("broker_order_id", deal.order)]:
            found = self._close_executions.get((key, value))
            if found and found.get("ticket") == deal.position_id:
                return found
        # Restore exact attribution after restart, using the existing JSON audit trail.
        matches = [ManagementAction.result["broker_deal_id"].astext == str(deal.ticket)]
        if deal.order > 0:
            matches.append(ManagementAction.result["broker_order_id"].astext == str(deal.order))
        try:
            async with database.get_session() as session:
                rows = await session.scalars(select(ManagementAction).where(
                    ManagementAction.status == "EXECUTED",
                    ManagementAction.action_type.in_(["CLOSE", "PARTIAL_CLOSE"]),
                    ManagementAction.parameters["ticket"].astext == str(deal.position_id),
                    or_(*matches)).order_by(ManagementAction.id.desc()))
                for action in rows.all():
                    metadata = action.result or {}
                    if metadata.get("close_cause") in CLOSE_CAUSES:
                        return metadata
        except Exception as exc:
            logger.warning("exit_context_unavailable", deal=deal.ticket, error=str(exc))
        return None

    async def exit_reason(self, deal):
        # SL can close in profit after moving the stop. Positive PnL never implies TP.
        if deal.reason in {DEAL_REASON_SL, DEAL_REASON_TP, DEAL_REASON_SO}:
            return BROKER_REASONS[deal.reason], None
        context = await self.exit_context(deal)
        if context and context.get("close_cause") in CLOSE_CAUSES:
            return CLOSE_CAUSES[context["close_cause"]], context
        return BROKER_REASONS.get(deal.reason, f"Chưa xác định · Mã MT5 {deal.reason}"), None

    async def format_exit_notification(self, deal, currency=""):
        reason, context = await self.exit_reason(deal)
        if deal.entry == "INOUT":
            title = "🔄 Deal đảo chiều vị thế"
            side = f"Deal {deal.deal_type} · đảo chiều"
        else:
            title = "✂️ Đã chốt một phần" if context and context.get("partial_close") else "📌 Đã ghi nhận lần đóng lệnh"
            side = "SELL" if deal.deal_type == "BUY" else "BUY" if deal.deal_type == "SELL" else deal.deal_type
        realized = deal.profit + deal.swap + deal.commission
        blocks = [heading(title, format_vietnam_time(deal.time, "%H:%M:%S · %d/%m/%Y")),
            section(f"{deal.symbol} · {side}",
                *([field("Mã lệnh", f"#{deal.position_id}", code=True)] if deal.position_id > 0 else [])),
            section("KẾT QUẢ LẦN CHỐT NÀY",
                f"{'🟢' if realized >= 0 else '🔴'} <b>PnL lần chốt: {escape(money(realized, currency, signed=True))}</b>",
                field("Lãi/lỗ giá", money(deal.profit, currency, signed=True)),
                field("Swap", money(deal.swap, currency, signed=True)) + " · "
                + field("Commission", money(deal.commission, currency, signed=True))),
            section("KHỐI LƯỢNG VÀ GIÁ", field("Khối lượng deal (gồm mở lại)" if deal.entry == "INOUT" else "Đã đóng", f"{number(deal.volume)} lot"),
                field("Giá đóng", price(deal.price), code=True)),
            section("LÝ DO THỰC HIỆN", escape(reason)),
            "<i>PnL gồm swap và commission lần chốt; chưa phân bổ phí mở lệnh.</i>"]
        link = self._order_message_links.get(deal.position_id)
        if not link:
            signal_id = self._ticket_to_signal_db_id.get(deal.position_id)
            link = self._signal_message_links.get(signal_id)
        return self.format_notification(join_cards(blocks), link), reason
