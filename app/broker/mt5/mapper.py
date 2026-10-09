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


def map_account_info(raw: Any) -> AccountInfo:
    """Convert MT5 account_info to domain AccountInfo."""
    trade_mode = "DEMO"
    if getattr(raw, "trade_mode", 0) == 2:
        trade_mode = "REAL"
    elif getattr(raw, "trade_mode", 0) == 1:
        trade_mode = "CONTEST"

    return AccountInfo(
        login=int(getattr(raw, "login", 0)),
        balance=Decimal(str(getattr(raw, "balance", 0.0))),
        equity=Decimal(str(getattr(raw, "equity", 0.0))),
        margin=Decimal(str(getattr(raw, "margin", 0.0))),
        free_margin=Decimal(str(getattr(raw, "margin_free", 0.0))),
        leverage=int(getattr(raw, "leverage", 1)),
        server=str(getattr(raw, "server", "")),
        currency=str(getattr(raw, "currency", "USD")),
        trade_mode=trade_mode,
        raw=raw._asdict() if hasattr(raw, "_asdict") else {},
    )


def map_tick_info(symbol: str, raw: Any) -> TickInfo:
    """Convert MT5 symbol_info_tick to domain TickInfo."""
    bid = Decimal(str(getattr(raw, "bid", 0.0)))
    ask = Decimal(str(getattr(raw, "ask", 0.0)))
    spread = ask - bid
    timestamp = getattr(raw, "time", 0)
    time_dt = datetime.fromtimestamp(timestamp) if timestamp else datetime.utcnow()

    return TickInfo(
        symbol=symbol,
        bid=bid,
        ask=ask,
        time=time_dt,
        spread_points=spread,
    )


def map_symbol_info(raw: Any) -> SymbolInfo:
    """Convert MT5 symbol_info to domain SymbolInfo."""
    return SymbolInfo(
        symbol=str(getattr(raw, "name", "")),
        digits=int(getattr(raw, "digits", 2)),
        point=Decimal(str(getattr(raw, "point", 0.01))),
        volume_min=Decimal(str(getattr(raw, "volume_min", 0.01))),
        volume_max=Decimal(str(getattr(raw, "volume_max", 100.0))),
        volume_step=Decimal(str(getattr(raw, "volume_step", 0.01))),
        trade_contract_size=Decimal(str(getattr(raw, "trade_contract_size", 100.0))),
    )


def _parse_order_type(t: int) -> str:
    types = ["BUY", "SELL", "BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP", "BUY_STOP_LIMIT", "SELL_STOP_LIMIT"]
    if 0 <= t < len(types):
        return types[t]
    return f"UNKNOWN_{t}"


def map_pending_order_info(raw: Any) -> PendingOrderInfo:
    """Convert MT5 order to domain PendingOrderInfo."""
    sl = getattr(raw, "sl", 0.0)
    tp = getattr(raw, "tp", 0.0)
    setup_time = getattr(raw, "time_setup", 0)
    time_dt = datetime.fromtimestamp(setup_time) if setup_time else datetime.utcnow()

    return PendingOrderInfo(
        ticket=int(getattr(raw, "ticket", 0)),
        symbol=str(getattr(raw, "symbol", "")),
        order_type=_parse_order_type(getattr(raw, "type", 0)),
        volume=Decimal(str(getattr(raw, "volume_initial", 0.0))),
        price=Decimal(str(getattr(raw, "price_open", 0.0))),
        sl=Decimal(str(sl)) if sl > 0 else None,
        tp=Decimal(str(tp)) if tp > 0 else None,
        comment=str(getattr(raw, "comment", "")),
        setup_time=time_dt,
    )


def map_position_info(raw: Any) -> PositionInfo:
    """Convert MT5 position structure to domain PositionInfo."""
    pos_type = "BUY" if getattr(raw, "type", 0) == 0 else "SELL"
    open_time = getattr(raw, "time", 0)
    open_time_dt = datetime.fromtimestamp(open_time) if open_time else datetime.utcnow()

    sl_val = getattr(raw, "sl", 0.0)
    tp_val = getattr(raw, "tp", 0.0)

    return PositionInfo(
        ticket=int(getattr(raw, "ticket", 0)),
        symbol=str(getattr(raw, "symbol", "")),
        volume=Decimal(str(getattr(raw, "volume", 0.0))),
        open_price=Decimal(str(getattr(raw, "price_open", 0.0))),
        current_price=Decimal(str(getattr(raw, "price_current", 0.0))),
        sl=Decimal(str(sl_val)) if sl_val else None,
        tp=Decimal(str(tp_val)) if tp_val else None,
        profit=Decimal(str(getattr(raw, "profit", 0.0))),
        swap=Decimal(str(getattr(raw, "swap", 0.0))),
        commission=Decimal(str(getattr(raw, "commission", 0.0))),
        position_type=pos_type,
        open_time=open_time_dt,
        magic=int(getattr(raw, "magic", 0)),
        comment=str(getattr(raw, "comment", "")),
    )


def map_deal_info(raw: Any) -> DealInfo:
    """Convert MT5 deal structure to domain DealInfo."""
    deal_type_int = getattr(raw, "type", 0)
    deal_type_str = "BUY" if deal_type_int == 0 else ("SELL" if deal_type_int == 1 else "OTHER")

    entry_int = getattr(raw, "entry", 0)
    entry_map = {0: "IN", 1: "OUT", 2: "INOUT", 3: "OUT_BY"}
    entry_str = entry_map.get(entry_int, "IN")

    timestamp = getattr(raw, "time", 0)
    time_dt = datetime.fromtimestamp(timestamp) if timestamp else datetime.utcnow()

    return DealInfo(
        ticket=int(getattr(raw, "ticket", 0)),
        order=int(getattr(raw, "order", 0)),
        position_id=int(getattr(raw, "position_id", 0)),
        symbol=str(getattr(raw, "symbol", "")),
        deal_type=deal_type_str,
        entry=entry_str,
        volume=Decimal(str(getattr(raw, "volume", 0.0))),
        price=Decimal(str(getattr(raw, "price", 0.0))),
        profit=Decimal(str(getattr(raw, "profit", 0.0))),
        swap=Decimal(str(getattr(raw, "swap", 0.0))),
        commission=Decimal(str(getattr(raw, "commission", 0.0))),
        time=time_dt,
        reason=int(getattr(raw, "reason", 0)),
        magic=int(getattr(raw, "magic", 0)),
        comment=str(getattr(raw, "comment", "")),
    )


def map_order_result(raw: Any) -> OrderResult:
    """Convert MT5 order_send result to domain OrderResult."""
    if raw is None:
        return OrderResult(
            success=False,
            comment="order_send returned None",
        )

    retcode = getattr(raw, "retcode", -1)
    success = retcode in (TRADE_RETCODE_DONE, TRADE_RETCODE_DONE_PARTIAL, TRADE_RETCODE_PLACED)
    comment = getattr(raw, "comment", "") or RETCODE_DESCRIPTIONS.get(retcode, f"Retcode {retcode}")

    order_ticket = getattr(raw, "order", None)
    price = getattr(raw, "price", None)
    volume = getattr(raw, "volume", None)

    return OrderResult(
        success=success,
        ticket=int(order_ticket) if order_ticket else None,
        retcode=retcode,
        price=Decimal(str(price)) if price is not None else None,
        volume=Decimal(str(volume)) if volume is not None else None,
        comment=comment,
        raw=raw._asdict() if hasattr(raw, "_asdict") else {},
    )
