"""Shared Telegram HTML presentation; no broker calls or trading decisions."""
from decimal import Decimal, InvalidOperation
from html import escape

DIVIDER = "━━━━━━━━━━━━━━━━"


def heading(title, subtitle=None):
    return f"<b>{escape(title)}</b>" + (f"\n<i>{escape(subtitle)}</i>" if subtitle else "")


def section(title, *lines):
    return f"<b>{escape(title)}</b>\n" + "\n".join(lines)


def field(label, value, *, code=False):
    text = escape(str(value))
    return f"{escape(label)}: " + (f"<code>{text}</code>" if code else text)


def number(value, *, minimum=0):
    if value is None:
        return "—"
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            return "—"
        decimals = max(minimum, min(8, -amount.normalize().as_tuple().exponent))
        return f"{amount:,.{decimals}f}"
    except (ValueError, InvalidOperation):
        return str(value)


def price(value):
    return number(value, minimum=2) if value else "—"


def money(value, currency="", *, signed=False):
    amount = Decimal(str(value))
    text = f"{amount:+,.2f}" if signed else f"{amount:,.2f}"
    return text + (f" {currency}" if currency else "")


def trade_title(item):
    side = getattr(item, "position_type", None) or item.order_type
    return f"{'🟢' if side.startswith('BUY') else '🔴'} {item.symbol} · {side}"


def position_card(pos, currency="", ordinal=None):
    prefix = f"{ordinal}. " if ordinal is not None else ""
    branch = (" · Min Entry" if pos.members and ('min-entry:' in pos.comment or 'override-min:' in pos.comment)
              else " · Max Entry" if pos.members and ('max-entry:' in pos.comment or 'override-max:' in pos.comment) else "")
    targets = (' · '.join(f"{'TP1' if p.comment.endswith(':tp1') else 'TP2'} {price(p.tp)} ({number(p.volume)} lot)" for p in pos.members)
               if pos.members else f"TP {price(pos.tp)}")
    return "\n".join([
        heading(prefix + trade_title(pos) + branch) + f" · <code>#{pos.ticket}</code>",
        f"PnL đang chạy <b>{escape(money(pos.profit, currency, signed=True))}</b> · {number(pos.volume)} lot",
        f"Entry {price(pos.open_price)} → Hiện tại {price(pos.current_price)}",
        f"SL {price(pos.sl)} · {targets}",
    ])


def pending_card(order):
    return "\n".join([
        heading("⏳ " + trade_title(order).split(" ", 1)[1]) + f" · <code>#{order.ticket}</code>",
        f"{number(order.volume)} lot · Giá đặt {price(order.price)}",
        f"SL {price(order.sl)} · TP {price(order.tp)}",
    ])


def join_cards(cards):
    return "\n\n".join(card for card in cards if card)


def history_card(pos):
    from app.core.timezone import format_vietnam_time
    status = ("Chưa xác định trạng thái" if pos.is_fully_closed is None else
              "Đã đóng hết" if pos.is_fully_closed else "Còn giữ vị thế")
    return "\n".join([
        heading(f"{pos.symbol} · {pos.position_type}") + f" · <code>#{pos.ticket}</code>",
        f"PnL đã chốt <b>{escape(money(pos.net_pnl, pos.currency, signed=True))}</b> · Đóng {number(pos.volume)}/{number(pos.total_volume)} lot",
        f"Entry {price(pos.open_price)} → Đóng TB {price(pos.close_price)}",
        f"{format_vietnam_time(pos.close_time, '%H:%M %d/%m/%Y')} · {status} · {pos.deals_count} lần chốt",
    ])


def config_value(value):
    if isinstance(value, bool):
        return "Bật" if value else "Tắt"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    labels = {"ticket": "Entry riêng từng lệnh", "breakeven": "Chỉ dời SL nếu đã bật tự dời", "skip": "Bỏ qua"}
    if isinstance(value, str) and value in labels:
        return labels[value]
    if isinstance(value, (Decimal, int, float)):
        return number(value)
    return str(value)


def execution_card(title, request, result, timestamp=None):
    filled = request.order_type in {"BUY", "SELL"}
    blocks = [heading(title),
        heading(f"{request.symbol} · {request.order_type}") + f" · <code>#{result.ticket}</code>\n"
        + f"{number(result.volume or request.volume)} lot · "
        + field("Giá khớp" if filled else "Giá đặt", price(result.price or request.price)) + "\n"
        + f"SL {price(request.sl)} · TP {price(request.tp)}"]
    if request.tp1 is not None and request.tp2 is not None:
        blocks.append(f"TP1 {price(request.tp1)} · TP2 {price(request.tp2)}")
    if result.raw.get('native_children'):
        blocks.append('TP trên MT5: ' + ' · '.join(f"{number(req.volume)} lot → {price(req.tp)}" for req, _ in result.raw['native_children']))
    if timestamp:
        blocks.append(f"<i>{escape(timestamp)}</i>")
    return join_cards(blocks)
