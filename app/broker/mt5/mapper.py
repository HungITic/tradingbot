"""
Mappers between MT5 raw structures and domain dataclasses.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from app.broker.base import (
    AccountInfo,
    DealInfo,
    OrderResult,
    PendingOrderInfo,
    PositionInfo,
    SymbolInfo,
    TickInfo,
)
from app.broker.mt5.constants import (
    RETCODE_DESCRIPTIONS,
    TRADE_RETCODE_DONE,
    TRADE_RETCODE_DONE_PARTIAL,
    TRADE_RETCODE_PLACED,
)
from app.core.timezone import broker_timestamp_to_datetime, detect_and_update_broker_offset


def _safe_int(val: Any, default: int = 0) -> int:
    """Safely convert any value to int, avoiding NoneType errors."""
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _safe_decimal(val: Any, default: str = "0.0") -> Decimal:
    """Safely convert any value to Decimal, avoiding NoneType errors."""
    if val is None:
        return Decimal(default)
    try:
        return Decimal(str(val))
    except Exception:
        return Decimal(default)


def map_account_info(raw: Any) -> AccountInfo:
    """Convert MT5 account_info to domain AccountInfo."""
    tm_raw = getattr(raw, "trade_mode", 0) if not isinstance(raw, dict) else raw.get("trade_mode", 0)
    tm_val = _safe_int(tm_raw, 0)
    trade_mode = "DEMO"
    if tm_val == 2:
        trade_mode = "REAL"
    elif tm_val == 1:
        trade_mode = "CONTEST"

    login = getattr(raw, "login", 0) if not isinstance(raw, dict) else raw.get("login", 0)
    balance = getattr(raw, "balance", 0.0) if not isinstance(raw, dict) else raw.get("balance", 0.0)
    equity = getattr(raw, "equity", 0.0) if not isinstance(raw, dict) else raw.get("equity", 0.0)
    margin = getattr(raw, "margin", 0.0) if not isinstance(raw, dict) else raw.get("margin", 0.0)
    free_margin = getattr(raw, "margin_free", 0.0) if not isinstance(raw, dict) else raw.get("margin_free", 0.0)
    leverage = getattr(raw, "leverage", 1) if not isinstance(raw, dict) else raw.get("leverage", 1)
    server = getattr(raw, "server", "") if not isinstance(raw, dict) else raw.get("server", "")
    currency = getattr(raw, "currency", "USD") if not isinstance(raw, dict) else raw.get("currency", "USD")

    return AccountInfo(
        login=_safe_int(login, 0),
        balance=_safe_decimal(balance, "0.0"),
        equity=_safe_decimal(equity, "0.0"),
        margin=_safe_decimal(margin, "0.0"),
        free_margin=_safe_decimal(free_margin, "0.0"),
        leverage=_safe_int(leverage, 1),
        server=str(server or ""),
        currency=str(currency or "USD"),
        trade_mode=trade_mode,
        raw=raw._asdict() if hasattr(raw, "_asdict") else (raw if isinstance(raw, dict) else {}),
    )


def map_tick_info(symbol: str, raw: Any) -> TickInfo:
    """Convert MT5 symbol_info_tick to domain TickInfo."""
    bid_raw = getattr(raw, "bid", 0.0) if not isinstance(raw, dict) else raw.get("bid", 0.0)
    ask_raw = getattr(raw, "ask", 0.0) if not isinstance(raw, dict) else raw.get("ask", 0.0)
    bid = _safe_decimal(bid_raw, "0.0")
    ask = _safe_decimal(ask_raw, "0.0")
    spread = ask - bid
    timestamp = getattr(raw, "time", 0) if not isinstance(raw, dict) else raw.get("time", 0)
    ts_int = _safe_int(timestamp, 0)
    detect_and_update_broker_offset(ts_int)
    time_dt = broker_timestamp_to_datetime(ts_int)

    return TickInfo(
        symbol=symbol,
        bid=bid,
        ask=ask,
        time=time_dt,
        spread_points=spread,
    )


def map_symbol_info(raw: Any) -> SymbolInfo:
    """Convert MT5 symbol_info to domain SymbolInfo."""
    name = getattr(raw, "name", "") if not isinstance(raw, dict) else raw.get("name", "")
    digits = getattr(raw, "digits", 2) if not isinstance(raw, dict) else raw.get("digits", 2)
    point = getattr(raw, "point", 0.01) if not isinstance(raw, dict) else raw.get("point", 0.01)
    vol_min = getattr(raw, "volume_min", 0.01) if not isinstance(raw, dict) else raw.get("volume_min", 0.01)
    vol_max = getattr(raw, "volume_max", 100.0) if not isinstance(raw, dict) else raw.get("volume_max", 100.0)
    vol_step = getattr(raw, "volume_step", 0.01) if not isinstance(raw, dict) else raw.get("volume_step", 0.01)
    tcs = getattr(raw, "trade_contract_size", 100.0) if not isinstance(raw, dict) else raw.get("trade_contract_size", 100.0)

    return SymbolInfo(
        symbol=str(name or ""),
        digits=_safe_int(digits, 2),
        point=_safe_decimal(point, "0.01"),
        volume_min=_safe_decimal(vol_min, "0.01"),
        volume_max=_safe_decimal(vol_max, "100.0"),
        volume_step=_safe_decimal(vol_step, "0.01"),
        trade_contract_size=_safe_decimal(tcs, "100.0"),
    )


def _parse_order_type(t: int) -> str:
    types = ["BUY", "SELL", "BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP", "BUY_STOP_LIMIT", "SELL_STOP_LIMIT"]
    if 0 <= t < len(types):
        return types[t]
    return f"UNKNOWN_{t}"


def map_pending_order_info(raw: Any) -> PendingOrderInfo:
    """Convert MT5 order to domain PendingOrderInfo."""
    sl = getattr(raw, "sl", None) if not isinstance(raw, dict) else raw.get("sl")
    tp = getattr(raw, "tp", None) if not isinstance(raw, dict) else raw.get("tp")
    setup_time = getattr(raw, "time_setup", 0) if not isinstance(raw, dict) else raw.get("time_setup", 0)
    time_dt = broker_timestamp_to_datetime(_safe_int(setup_time, 0))

    ticket = getattr(raw, "ticket", 0) if not isinstance(raw, dict) else raw.get("ticket", 0)
    symbol = getattr(raw, "symbol", "") if not isinstance(raw, dict) else raw.get("symbol", "")
    type_val = getattr(raw, "type", 0) if not isinstance(raw, dict) else raw.get("type", 0)
    volume = getattr(raw, "volume_initial", 0.0) if not isinstance(raw, dict) else raw.get("volume_initial", 0.0)
    price = getattr(raw, "price_open", 0.0) if not isinstance(raw, dict) else raw.get("price_open", 0.0)
    comment = getattr(raw, "comment", "") if not isinstance(raw, dict) else raw.get("comment", "")

    return PendingOrderInfo(
        ticket=_safe_int(ticket, 0),
        symbol=str(symbol or ""),
        order_type=_parse_order_type(_safe_int(type_val, 0)),
        volume=_safe_decimal(volume, "0.0"),
        price=_safe_decimal(price, "0.0"),
        sl=Decimal(str(sl)) if sl and float(sl) > 0 else None,
        tp=Decimal(str(tp)) if tp and float(tp) > 0 else None,
        comment=str(comment or ""),
        setup_time=time_dt,
    )


def map_position_info(raw: Any) -> PositionInfo:
    """Convert MT5 position structure to domain PositionInfo."""
    type_raw = getattr(raw, "type", 0) if not isinstance(raw, dict) else raw.get("type", 0)
    pos_type = "BUY" if _safe_int(type_raw, 0) == 0 else "SELL"
    open_time = getattr(raw, "time", 0) if not isinstance(raw, dict) else raw.get("time", 0)
    open_time_dt = broker_timestamp_to_datetime(_safe_int(open_time, 0))

    sl_val = getattr(raw, "sl", None) if not isinstance(raw, dict) else raw.get("sl")
    tp_val = getattr(raw, "tp", None) if not isinstance(raw, dict) else raw.get("tp")

    ticket = getattr(raw, "ticket", 0) if not isinstance(raw, dict) else raw.get("ticket", 0)
    symbol = getattr(raw, "symbol", "") if not isinstance(raw, dict) else raw.get("symbol", "")
    volume = getattr(raw, "volume", 0.0) if not isinstance(raw, dict) else raw.get("volume", 0.0)
    open_price = getattr(raw, "price_open", 0.0) if not isinstance(raw, dict) else raw.get("price_open", 0.0)
    curr_price = getattr(raw, "price_current", 0.0) if not isinstance(raw, dict) else raw.get("price_current", 0.0)
    profit = getattr(raw, "profit", 0.0) if not isinstance(raw, dict) else raw.get("profit", 0.0)
    swap = getattr(raw, "swap", 0.0) if not isinstance(raw, dict) else raw.get("swap", 0.0)
    comm = getattr(raw, "commission", 0.0) if not isinstance(raw, dict) else raw.get("commission", 0.0)
    magic = getattr(raw, "magic", 0) if not isinstance(raw, dict) else raw.get("magic", 0)
    comment = getattr(raw, "comment", "") if not isinstance(raw, dict) else raw.get("comment", "")

    return PositionInfo(
        ticket=_safe_int(ticket, 0),
        symbol=str(symbol or ""),
        volume=_safe_decimal(volume, "0.0"),
        open_price=_safe_decimal(open_price, "0.0"),
        current_price=_safe_decimal(curr_price, "0.0"),
        sl=Decimal(str(sl_val)) if sl_val else None,
        tp=Decimal(str(tp_val)) if tp_val else None,
        profit=_safe_decimal(profit, "0.0"),
        swap=_safe_decimal(swap, "0.0"),
        commission=_safe_decimal(comm, "0.0"),
        position_type=pos_type,
        open_time=open_time_dt,
        identifier=_safe_int(raw.get("identifier", 0) if isinstance(raw, dict) else getattr(raw, "identifier", 0)),
        magic=_safe_int(magic, 0),
        comment=str(comment or ""),
    )


def map_deal_info(raw: Any) -> DealInfo:
    """Convert MT5 deal structure to domain DealInfo."""
    deal_type_raw = getattr(raw, "type", 0) if not isinstance(raw, dict) else raw.get("type", 0)
    deal_type_int = _safe_int(deal_type_raw, 0)
    deal_type_str = "BUY" if deal_type_int == 0 else ("SELL" if deal_type_int == 1 else "OTHER")

    entry_raw = getattr(raw, "entry", 0) if not isinstance(raw, dict) else raw.get("entry", 0)
    entry_int = _safe_int(entry_raw, 0)
    entry_map = {0: "IN", 1: "OUT", 2: "INOUT", 3: "OUT_BY"}
    entry_str = entry_map.get(entry_int, "IN")

    timestamp_raw = getattr(raw, "time", 0) if not isinstance(raw, dict) else raw.get("time", 0)
    time_dt = broker_timestamp_to_datetime(_safe_int(timestamp_raw, 0))

    if isinstance(raw, dict):
        pos_id = raw.get("position_id") or raw.get("position") or 0
        ticket = raw.get("ticket") or 0
        order = raw.get("order") or 0
        symbol = raw.get("symbol") or ""
        volume = raw.get("volume") or 0.0
        price = raw.get("price") or 0.0
        profit = raw.get("profit") or 0.0
        swap = raw.get("swap") or 0.0
        commission = raw.get("commission") or 0.0
        reason = raw.get("reason") or 0
        magic = raw.get("magic") or 0
        comment = raw.get("comment") or ""
    else:
        # mt5_mac uses `position` while official MetaTrader5 uses `position_id`
        pos_id = getattr(raw, "position_id", None)
        if pos_id is None or _safe_int(pos_id, 0) == 0:
            pos_id = getattr(raw, "position", 0)
        ticket = getattr(raw, "ticket", 0)
        order = getattr(raw, "order", 0)
        symbol = getattr(raw, "symbol", "")
        volume = getattr(raw, "volume", 0.0)
        price = getattr(raw, "price", 0.0)
        profit = getattr(raw, "profit", 0.0)
        swap = getattr(raw, "swap", 0.0)
        commission = getattr(raw, "commission", 0.0)
        reason = getattr(raw, "reason", 0)
        magic = getattr(raw, "magic", 0)
        comment = getattr(raw, "comment", "")

    return DealInfo(
        ticket=_safe_int(ticket, 0),
        order=_safe_int(order, 0),
        position_id=_safe_int(pos_id, 0),
        symbol=str(symbol or ""),
        deal_type=deal_type_str,
        entry=entry_str,
        volume=_safe_decimal(volume, "0.0"),
        price=_safe_decimal(price, "0.0"),
        profit=_safe_decimal(profit, "0.0"),
        swap=_safe_decimal(swap, "0.0"),
        commission=_safe_decimal(commission, "0.0"),
        time=time_dt,
        reason=_safe_int(reason, 0),
        magic=_safe_int(magic, 0),
        comment=str(comment or ""),
    )


def map_order_result(raw: Any) -> OrderResult:
    """Convert MT5 order_send result to domain OrderResult."""
    if raw is None:
        return OrderResult(
            success=False,
            comment="order_send returned None",
        )

    retcode_raw = getattr(raw, "retcode", -1) if not isinstance(raw, dict) else raw.get("retcode", -1)
    retcode = _safe_int(retcode_raw, -1)
    success = retcode in (TRADE_RETCODE_DONE, TRADE_RETCODE_DONE_PARTIAL, TRADE_RETCODE_PLACED)
    comment = (getattr(raw, "comment", "") if not isinstance(raw, dict) else raw.get("comment", "")) or RETCODE_DESCRIPTIONS.get(retcode, f"Retcode {retcode}")

    order_ticket = getattr(raw, "order", None) if not isinstance(raw, dict) else raw.get("order")
    price = getattr(raw, "price", None) if not isinstance(raw, dict) else raw.get("price")
    volume = getattr(raw, "volume", None) if not isinstance(raw, dict) else raw.get("volume")

    return OrderResult(
        success=success,
        ticket=_safe_int(order_ticket) if order_ticket else None,
        retcode=retcode,
        price=_safe_decimal(price) if price is not None else None,
        volume=_safe_decimal(volume) if volume is not None else None,
        comment=str(comment or ""),
        raw=raw._asdict() if hasattr(raw, "_asdict") else (raw if isinstance(raw, dict) else {
            key: getattr(raw, key, None) for key in ("order", "deal", "retcode", "price", "volume", "comment")
        }),
    )
