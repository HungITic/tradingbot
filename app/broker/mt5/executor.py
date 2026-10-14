"""
MT5 Executor implementing the abstract BrokerAdapter interface.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog

from app.broker.base import (
    AccountInfo,
    BrokerAdapter,
    DealInfo,
    OrderRequest,
    OrderResult,
    PendingOrderInfo,
    PositionInfo,
    SymbolInfo,
    TickInfo,
)
from app.broker.mt5.client import mt5, mt5_client
from app.broker.mt5.constants import (
    ORDER_FILLING_IOC,
    ORDER_FILLING_RETURN,
    ORDER_TIME_GTC,
    ORDER_TYPE_BUY,
    ORDER_TYPE_SELL,
    ORDER_TYPE_BUY_LIMIT,
    ORDER_TYPE_SELL_LIMIT,
    ORDER_TYPE_BUY_STOP,
    ORDER_TYPE_SELL_STOP,
    TRADE_ACTION_DEAL,
    TRADE_ACTION_PENDING,
    TRADE_ACTION_SLTP,
)
from app.broker.mt5.mapper import (
    map_account_info,
    map_deal_info,
    map_order_result,
    map_pending_order_info,
    map_position_info,
    map_symbol_info,
    map_tick_info,
)

logger = structlog.get_logger(__name__)


class MT5Executor(BrokerAdapter):
    """Execution adapter for MetaTrader 5 via local IPC."""

    def __init__(self) -> None:
        self._client = mt5_client
        self._symbol_cache: dict[str, str] = {}

    async def connect(self) -> bool:
        return await self._client.connect()

    async def disconnect(self) -> None:
        await self._client.disconnect()

    async def is_connected(self) -> bool:
        return await self._client.ping()

    async def resolve_symbol(self, symbol: str) -> str:
        """
        Dynamically resolve broker-specific symbol names (e.g. XAUUSD -> XAUUSDm on Exness).
        Caches results for high performance.
        """
        if not symbol:
            return symbol

        clean = symbol.strip().replace("#", "")
        clean_upper = clean.upper()

        if clean_upper in self._symbol_cache:
            return self._symbol_cache[clean_upper]

        def _is_tradeable(sym_info: Any) -> bool:
            if sym_info is None:
                return False
            trade_mode = getattr(sym_info, "trade_mode", 4)
            # trade_mode 0 is SYMBOL_TRADE_MODE_DISABLED in MT5
            return trade_mode != 0

        # 1. Direct check in MT5
        raw_info = await self._client.run_in_thread(mt5.symbol_info, clean)
        if _is_tradeable(raw_info):
            await self._client.run_in_thread(mt5.symbol_select, raw_info.name, True)
            self._symbol_cache[clean_upper] = raw_info.name
            return raw_info.name

        if clean != clean_upper:
            raw_info = await self._client.run_in_thread(mt5.symbol_info, clean_upper)
            if _is_tradeable(raw_info):
                await self._client.run_in_thread(mt5.symbol_select, raw_info.name, True)
                self._symbol_cache[clean_upper] = raw_info.name
                return raw_info.name

        # 2. Check candidate suffixes commonly used by brokers (Cent, Standard, Pro, etc.)
        candidates: list[str] = [
            f"{clean_upper}.c",     # Cent with dot (PU Prime, XM, etc.)
            f"{clean_upper}c",      # Cent (e.g. XAUUSDc)
            f"{clean_upper}.m",    # Micro / Cent
            f"{clean_upper}m",      # Exness Standard/Trial (e.g. XAUUSDm)
            f"{clean_upper}.s",    # Zero spread
            f"{clean_upper}.raw",  # Raw spread
            f"{clean_upper}pro",   # Pro
            f"{clean_upper}+",     # VIP
            f"{clean_upper}_i",    # Index / Institutional
        ]

        # If symbol already has suffix, also candidate stripped base
        for suf in ("M", "C", ".M", ".C", ".RAW", "PRO", "_I", ".S", "+", ".CRP", "CRP"):
            if clean_upper.endswith(suf):
                candidates.append(clean_upper[: -len(suf)])

        for cand in candidates:
            cand_info = await self._client.run_in_thread(mt5.symbol_info, cand)
            if _is_tradeable(cand_info):
                await self._client.run_in_thread(mt5.symbol_select, cand_info.name, True)
                logger.info("symbol_resolved_candidate", original=symbol, resolved=cand_info.name)
                self._symbol_cache[clean_upper] = cand_info.name
                return cand_info.name

        # 3. Fallback: Search all symbols in MT5
        try:
            all_syms = await self._client.run_in_thread(mt5.symbols_get)
            if all_syms:
                # Filter ONLY tradeable symbols (exclude disabled symbols like .crp)
                tradeable_syms = [s for s in all_syms if _is_tradeable(s)]
                base_name = clean_upper.rstrip("M").rstrip("C")
                matching = []
                for s in tradeable_syms:
                    s_name = s.name.upper()
                    if s_name.startswith(clean_upper) or (len(base_name) >= 3 and s_name.startswith(base_name)):
                        matching.append(s)

                if matching:
                    # Sort: prefer visible symbols in Market Watch, and deprioritize .crp
                    matching.sort(key=lambda s: (not getattr(s, "visible", False), ".crp" in s.name.lower()))
                    best = matching[0]
                    await self._client.run_in_thread(mt5.symbol_select, best.name, True)
                    logger.info("symbol_resolved_search", original=symbol, resolved=best.name)
                    self._symbol_cache[clean_upper] = best.name
                    return best.name
        except Exception as e:
            logger.warning("symbol_search_failed", symbol=symbol, error=str(e))

        # Default fallback
        self._symbol_cache[clean_upper] = clean
        return clean

    async def get_account_info(self) -> AccountInfo:
        if not self._client.is_connected or mt5 is None:
            raise RuntimeError("MT5 is not connected")
        try:
            raw = await self._client.run_in_thread(mt5.account_info)
        except Exception as e:
            self._client._connected = False
            raise RuntimeError(f"Failed to fetch account_info: {e}")
        if raw is None:
            err = mt5.last_error() if hasattr(mt5, "last_error") else "unknown"
            raise RuntimeError(f"Failed to fetch account_info: {err}")
        return map_account_info(raw)

    async def get_tick(self, symbol: str) -> TickInfo:
        if not self._client.is_connected or mt5 is None:
            raise RuntimeError("MT5 is not connected")
        resolved = await self.resolve_symbol(symbol)
        try:
            # Ensure symbol is active in Market Watch
            await self._client.run_in_thread(mt5.symbol_select, resolved, True)
            raw = await self._client.run_in_thread(mt5.symbol_info_tick, resolved)
            if raw is None and resolved != symbol:
                await self._client.run_in_thread(mt5.symbol_select, symbol, True)
                raw = await self._client.run_in_thread(mt5.symbol_info_tick, symbol)
                if raw is not None:
                    resolved = symbol
        except Exception as e:
            self._client._connected = False
            raise RuntimeError(f"Failed to fetch tick for {resolved}: {e}")
        if raw is None:
            err = mt5.last_error() if hasattr(mt5, "last_error") else "unknown"
            raise RuntimeError(f"Failed to fetch tick for {resolved} (requested: {symbol}): {err}")
        return map_tick_info(resolved, raw)

    async def get_symbol_info(self, symbol: str) -> SymbolInfo:
        if not self._client.is_connected or mt5 is None:
            raise RuntimeError("MT5 is not connected")
        resolved = await self.resolve_symbol(symbol)
        try:
            await self._client.run_in_thread(mt5.symbol_select, resolved, True)
            raw = await self._client.run_in_thread(mt5.symbol_info, resolved)
            if raw is None and resolved != symbol:
                await self._client.run_in_thread(mt5.symbol_select, symbol, True)
                raw = await self._client.run_in_thread(mt5.symbol_info, symbol)
                if raw is not None:
                    resolved = symbol
        except Exception as e:
            self._client._connected = False
            raise RuntimeError(f"Failed to fetch symbol_info for {resolved}: {e}")
        if raw is None:
            err = mt5.last_error() if hasattr(mt5, "last_error") else "unknown"
            raise RuntimeError(f"Failed to fetch symbol_info for {resolved} (requested: {symbol}): {err}")
        return map_symbol_info(raw)

    async def get_positions(self, symbol: str | None = None) -> list[PositionInfo]:
        if not self._client.is_connected or mt5 is None:
            return []
        try:
            raw_list = await self._client.run_in_thread(mt5.positions_get)
        except Exception as e:
            logger.warning("mt5_get_positions_failed", error=str(e))
            self._client._connected = False
            return []
        if raw_list is None:
            return []

        all_positions = [map_position_info(pos) for pos in raw_list]
        if not symbol:
            return all_positions

        resolved = await self.resolve_symbol(symbol)
        req_clean = symbol.strip().upper().replace("#", "")
        res_clean = resolved.strip().upper().replace("#", "")
        base_prefix = req_clean.rstrip("M").rstrip("C").rstrip(".M")

        matching: list[PositionInfo] = []
        for pos in all_positions:
            pos_sym = pos.symbol.upper()
            if (
                pos_sym == req_clean
                or pos_sym == res_clean
                or (len(base_prefix) >= 3 and pos_sym.startswith(base_prefix))
                or (base_prefix.startswith("XAU") and "XAU" in pos_sym)
            ):
                matching.append(pos)
        return matching

    async def get_pending_orders(self, symbol: str | None = None) -> list[PendingOrderInfo]:
        if not self._client.is_connected or mt5 is None:
            return []
        try:
            raw_list = await self._client.run_in_thread(mt5.orders_get)
        except Exception as e:
            logger.warning("mt5_get_orders_failed", error=str(e))
            self._client._connected = False
            return []
        if raw_list is None:
            return []

        all_orders = [map_pending_order_info(order) for order in raw_list]
        if not symbol:
            return all_orders

        resolved = await self.resolve_symbol(symbol)
        req_clean = symbol.strip().upper().replace("#", "")
        res_clean = resolved.strip().upper().replace("#", "")
        base_prefix = req_clean.rstrip("M").rstrip("C").rstrip(".M")

        matching: list[PendingOrderInfo] = []
        for order in all_orders:
            order_sym = order.symbol.upper()
            if (
                order_sym == req_clean
                or order_sym == res_clean
                or (len(base_prefix) >= 3 and order_sym.startswith(base_prefix))
                or (base_prefix.startswith("XAU") and "XAU" in order_sym)
            ):
                matching.append(order)
        return matching

    async def open_order(self, request: OrderRequest) -> OrderResult:
        if not self._client.is_connected or mt5 is None:
            return OrderResult(success=False, comment="MT5 is not connected")

        resolved_symbol = await self.resolve_symbol(request.symbol)
        # Fetch current price if not supplied
        tick = await self.get_tick(resolved_symbol)

        ot = request.order_type.upper()
        if ot == "BUY":
            order_type_int = ORDER_TYPE_BUY
            action = TRADE_ACTION_DEAL
            filling = ORDER_FILLING_IOC
            exec_price = float(request.price) if request.price else float(tick.ask)
        elif ot == "SELL":
            order_type_int = ORDER_TYPE_SELL
            action = TRADE_ACTION_DEAL
            filling = ORDER_FILLING_IOC
            exec_price = float(request.price) if request.price else float(tick.bid)
        elif ot == "BUY_LIMIT":
            order_type_int = ORDER_TYPE_BUY_LIMIT
            action = TRADE_ACTION_PENDING
            filling = ORDER_FILLING_RETURN
            exec_price = float(request.price) if request.price else float(tick.ask)
        elif ot == "SELL_LIMIT":
            order_type_int = ORDER_TYPE_SELL_LIMIT
            action = TRADE_ACTION_PENDING
            filling = ORDER_FILLING_RETURN
            exec_price = float(request.price) if request.price else float(tick.bid)
        elif ot == "BUY_STOP":
            order_type_int = ORDER_TYPE_BUY_STOP
            action = TRADE_ACTION_PENDING
            filling = ORDER_FILLING_RETURN
            exec_price = float(request.price) if request.price else float(tick.ask)
        elif ot == "SELL_STOP":
            order_type_int = ORDER_TYPE_SELL_STOP
            action = TRADE_ACTION_PENDING
            filling = ORDER_FILLING_RETURN
            exec_price = float(request.price) if request.price else float(tick.bid)
        else:
            order_type_int = ORDER_TYPE_BUY if "BUY" in ot else ORDER_TYPE_SELL
            action = TRADE_ACTION_DEAL
            filling = ORDER_FILLING_IOC
            exec_price = float(request.price) if request.price else float(
                tick.ask if order_type_int == ORDER_TYPE_BUY else tick.bid
            )

        req_dict: dict[str, Any] = {
            "action": action,
            "symbol": resolved_symbol,
            "volume": float(request.volume),
            "type": order_type_int,
            "price": exec_price,
            "deviation": request.deviation,
            "magic": request.magic,
            "comment": request.comment or "trading-bot-order",
            "type_time": ORDER_TIME_GTC,
            "type_filling": filling,
        }

        if request.sl is not None:
            req_dict["sl"] = float(request.sl)
        if request.tp is not None:
            req_dict["tp"] = float(request.tp)

        logger.info("mt5_sending_order", request=req_dict)
        try:
            raw_res = await self._client.run_in_thread(mt5.order_send, req_dict)
            result = map_order_result(raw_res)
        except Exception as e:
            logger.error("mt5_order_send_exception", error=str(e))
            self._client._connected = False
            return OrderResult(success=False, comment=f"Order send failed: {e}")

        if result.success:
            logger.info("mt5_order_success", ticket=result.ticket, price=result.price)
        else:
            logger.warning("mt5_order_failed", retcode=result.retcode, comment=result.comment)

        return result

    async def modify_position(
        self,
        ticket: int,
        sl: Decimal | None = None,
        tp: Decimal | None = None,
    ) -> OrderResult:
        if not self._client.is_connected or mt5 is None:
            return OrderResult(success=False, ticket=ticket, comment="MT5 is not connected")

        # Need symbol for position modify
        positions = await self.get_positions()
        target_pos = next((p for p in positions if p.ticket == ticket), None)
        if not target_pos:
            return OrderResult(
                success=False,
                ticket=ticket,
                comment=f"Position {ticket} not found in open positions",
            )

        req_dict: dict[str, Any] = {
            "action": TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": target_pos.symbol,
            "sl": float(sl) if sl is not None else float(target_pos.sl or 0.0),
            "tp": float(tp) if tp is not None else float(target_pos.tp or 0.0),
        }

        logger.info("mt5_modifying_position", ticket=ticket, sl=sl, tp=tp)
        try:
            raw_res = await self._client.run_in_thread(mt5.order_send, req_dict)
            return map_order_result(raw_res)
        except Exception as e:
            logger.error("mt5_modify_position_exception", error=str(e))
            self._client._connected = False
            return OrderResult(success=False, ticket=ticket, comment=f"Modify position failed: {e}")

    async def cancel_order(self, ticket: int) -> OrderResult:
        if not self._client.is_connected or mt5 is None:
            return OrderResult(success=False, ticket=ticket, comment="MT5 is not connected")

        from app.broker.mt5.constants import TRADE_ACTION_REMOVE
        req_dict = {
            "action": TRADE_ACTION_REMOVE,
            "order": ticket,
        }
        logger.info("mt5_canceling_order", ticket=ticket)
        try:
            raw_res = await self._client.run_in_thread(mt5.order_send, req_dict)
            return map_order_result(raw_res)
        except Exception as e:
            logger.error("mt5_cancel_order_exception", error=str(e))
            self._client._connected = False
            return OrderResult(success=False, ticket=ticket, comment=f"Cancel order failed: {e}")

    async def close_position(
        self,
        ticket: int,
        volume: Decimal | None = None,
    ) -> OrderResult:
        if not self._client.is_connected or mt5 is None:
            return OrderResult(success=False, ticket=ticket, comment="MT5 is not connected")

        positions = await self.get_positions()
        target_pos = next((p for p in positions if p.ticket == ticket), None)
        if not target_pos:
            return OrderResult(
                success=False,
                ticket=ticket,
                comment=f"Position {ticket} not found to close",
            )

        close_volume = volume if volume is not None else target_pos.volume
        tick = await self.get_tick(target_pos.symbol)

        # Opposite order type to close
        close_type = (
            ORDER_TYPE_SELL if target_pos.position_type == "BUY" else ORDER_TYPE_BUY
        )
        close_price = float(tick.bid if close_type == ORDER_TYPE_SELL else tick.ask)

        req_dict: dict[str, Any] = {
            "action": TRADE_ACTION_DEAL,
            "position": ticket,
            "symbol": target_pos.symbol,
            "volume": float(close_volume),
            "type": close_type,
            "price": close_price,
            "deviation": 20,
            "magic": target_pos.magic or 234000,
            "comment": f"trading-bot-close-{ticket}",
            "type_time": ORDER_TIME_GTC,
            "type_filling": ORDER_FILLING_IOC,
        }

        logger.info("mt5_closing_position", ticket=ticket, volume=close_volume)
        try:
            raw_res = await self._client.run_in_thread(mt5.order_send, req_dict)
            return map_order_result(raw_res)
        except Exception as e:
            logger.error("mt5_close_position_exception", error=str(e))
            self._client._connected = False
            return OrderResult(success=False, ticket=ticket, comment=f"Close position failed: {e}")

    async def get_history_deals(
        self, days: int = 7, symbol: str | None = None, start_time: datetime | None = None
    ) -> list[DealInfo]:
        """Fetch historical executed deals within the last `days` days."""
        if not self._client.is_connected or mt5 is None:
            return []

        now = datetime.now()
        if start_time:
            past = start_time
        else:
            past = now - timedelta(days=days)
        # Broker Time could be offset from Local Time by several hours.
        # By padding the Unix timestamp range by +/- 1 day, we ensure we cover the window.
        from_ts = int((past - timedelta(days=1)).timestamp())
        to_ts = int((now + timedelta(days=1)).timestamp())

        raw_list = None
        try:
            # mt5_mac JSON-RPC requires int timestamps; native MetaTrader5 accepts either
            raw_list = await self._client.run_in_thread(mt5.history_deals_get, from_ts, to_ts)
        except Exception:
            try:
                raw_list = await self._client.run_in_thread(mt5.history_deals_get, past, now)
            except Exception as e2:
                logger.warning("mt5_history_deals_failed", error=str(e2))
                self._client._connected = False
                return []

        if not raw_list:
            return []

        deals = [map_deal_info(d) for d in raw_list]
        if symbol:
            resolved = await self.resolve_symbol(symbol)
            req_clean = symbol.strip().upper().replace("#", "")
            res_clean = resolved.strip().upper().replace("#", "")
            base_prefix = req_clean.rstrip("M").rstrip("C").rstrip(".M")
            deals = [
                d
                for d in deals
                if (
                    d.symbol.upper() == req_clean
                    or d.symbol.upper() == res_clean
                    or (len(base_prefix) >= 3 and d.symbol.upper().startswith(base_prefix))
                )
            ]
        return deals

