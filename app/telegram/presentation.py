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
    pnl = money(pos.profit, currency, signed=True)
    return "\n\n".join([
        heading(prefix + trade_title(pos)) + "\n" + field("Ticket", f"#{pos.ticket}", code=True),
        f"{'🟢' if pos.profit >= 0 else '🔴'} <b>Lãi/lỗ đang chạy: {escape(pnl)}</b>\n"
        + field("Khối lượng", f"{number(pos.volume)} lot"),
        section("GIÁ GIAO DỊCH", field("Entry", price(pos.open_price), code=True),
                field("Hiện tại", price(pos.current_price), code=True)),
        section("BẢO VỆ LỆNH", field("SL · Dừng lỗ", price(pos.sl), code=True),
                field("TP · Chốt lời", price(pos.tp), code=True)),
    ])


def pending_card(order):
    return "\n\n".join([
        heading("⏳ " + trade_title(order).split(" ", 1)[1]) + "\n" + field("Ticket", f"#{order.ticket}", code=True),
        field("Khối lượng chờ", f"{number(order.volume)} lot"),
        section("MỨC GIÁ", field("Giá đặt", price(order.price), code=True),
                field("SL · Dừng lỗ", price(order.sl), code=True),
                field("TP · Chốt lời", price(order.tp), code=True)),
    ])


def join_cards(cards):
    return f"\n\n{DIVIDER}\n\n".join(cards)


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
        section(f"{request.symbol} · {request.order_type}",
            field("Ticket", f"#{result.ticket}", code=True),
            field("Khối lượng", f"{number(result.volume or request.volume)} lot")),
        section("MỨC GIÁ", field("Giá khớp" if filled else "Giá đặt", price(result.price or request.price), code=True),
            field("SL · Dừng lỗ", price(request.sl), code=True),
            field("TP · Chốt lời", price(request.tp), code=True))]
    if request.tp1 is not None and request.tp2 is not None:
        blocks.append(section("CHỐT THEO MỐC", field("TP1", price(request.tp1), code=True),
                              field("TP2", price(request.tp2), code=True)))
    if timestamp:
        blocks.append(f"<i>{escape(timestamp)}</i>")
    return join_cards(blocks)
