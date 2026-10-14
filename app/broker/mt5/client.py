"""
MT5 connection client wrapper.

Handles initializing connection to MT5 terminal, authentication,
and threadpool offloading so synchronous MT5 IPC calls don't block asyncio.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Callable

import structlog

from app.config.settings import settings

logger = structlog.get_logger(__name__)


CLEAN_AGENT_CODE = """\"\"\"
Agent script for mt5_mac library.
Runs inside Wine on macOS, using the official MetaTrader5 Python package.
Communicates via JSON over stdin/stdout.
\"\"\"

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any

try:
    import MetaTrader5 as mt5
except ImportError:
    print(json.dumps({"error": "MetaTrader5 not available in this Wine Python environment"}))
    sys.exit(1)

TERMINAL_PATH = os.environ.get(
    "MT5_TERMINAL_PATH",
    r"C:\\Program Files\\MetaTrader 5\\terminal64.exe",
)


def _serialize(obj: Any) -> Any:
    if obj is None:
        return None
    if hasattr(obj, "_asdict"):
        return _serialize(obj._asdict())
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(v) for v in obj]
    if hasattr(obj, "dtype"):
        return _serialize_array(obj)
    if hasattr(obj, "tolist"):
        return _serialize(obj.tolist())
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, float):
        if obj != obj:
            return None
        return obj
    if hasattr(obj, "item"):
        return obj.item()
    return obj


def _serialize_array(arr: Any) -> list[dict]:
    names = arr.dtype.names
    result = []
    for row in arr:
        item = {}
        for name in names:
            val = row[name]
            if isinstance(val, bytes):
                val = val.decode("utf-8", errors="replace")
            elif hasattr(val, "item"):
                val = val.item()
            item[name] = val
        result.append(item)
    return result


def handle(cmd: dict) -> dict:
    action = cmd.get("action", "")

    if action == "initialize":
        path = cmd.get("path") or TERMINAL_PATH
        portable = cmd.get("portable", True)
        kwargs = {"path": path, "portable": portable}
        if "login" in cmd and cmd["login"]:
            kwargs["login"] = int(cmd["login"])
        if "password" in cmd and cmd["password"]:
            kwargs["password"] = str(cmd["password"])
        if "server" in cmd and cmd["server"]:
            kwargs["server"] = str(cmd["server"])

        success = mt5.initialize(**kwargs)
        if not success:
            success = mt5.initialize(portable=portable)
        return {"success": success, "last_error": mt5.last_error()}

    if action == "login":
        success = mt5.login(int(cmd["login"]), cmd["password"], cmd.get("server", ""))
        return {"success": success}

    if action == "shutdown":
        mt5.shutdown()
        return {"success": True}

    if action == "terminal_info":
        info = mt5.terminal_info()
        return _serialize(info) if info else {}

    if action == "account_info":
        info = mt5.account_info()
        return _serialize(info) if info else {}

    if action == "symbols_get":
        symbol = cmd.get("symbol")
        symbols = mt5.symbols_get(symbol) if symbol else mt5.symbols_get()
        if symbols:
            return {"symbols": _serialize(symbols)}
        return {"symbols": []}

    if action == "symbol_info":
        info = mt5.symbol_info(cmd["symbol"])
        return _serialize(info) if info else {}

    if action == "symbol_select":
        return {"success": mt5.symbol_select(cmd["symbol"], cmd.get("enable", True))}

    if action == "tick_info":
        tick = mt5.symbol_info_tick(cmd["symbol"])
        if tick is None:
            mt5.symbol_select(cmd["symbol"], True)
            tick = mt5.symbol_info_tick(cmd["symbol"])
        return _serialize(tick) if tick else {}

    if action == "rates_get":
        rates = mt5.copy_rates_from_pos(
            cmd["symbol"],
            cmd.get("timeframe", 1),
            cmd.get("start_pos", 0),
            cmd.get("count", 100),
        )
        return {"rates": _serialize(rates)}

    if action == "copy_rates_from":
        rates = mt5.copy_rates_from(cmd["symbol"], cmd["timeframe"], cmd["date_from"], cmd["count"])
        return {"rates": _serialize(rates)}

    if action == "copy_rates_range":
        rates = mt5.copy_rates_range(cmd["symbol"], cmd["timeframe"], cmd["date_from"], cmd["date_to"])
        return {"rates": _serialize(rates)}

    if action == "order_send":
        result = mt5.order_send(cmd["request"])
        return _serialize(result) if result else {}

    if action == "positions_get":
        symbol = cmd.get("symbol", "")
        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        return {"positions": _serialize(positions)}

    if action == "orders_get":
        symbol = cmd.get("symbol", "")
        orders = mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()
        return {"orders": _serialize(orders)}

    if action == "history_deals":
        deals = mt5.history_deals_get(cmd.get("from", 0), cmd.get("to", 0))
        return {"deals": _serialize(deals)}

    if action == "history_orders":
        orders = mt5.history_orders_get(cmd.get("from", 0), cmd.get("to", 0))
        return {"orders": _serialize(orders)}

    return {"error": f"Unknown action: {action}"}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            result = handle(json.loads(line))
        except Exception as e:
            result = {"error": str(e), "traceback": traceback.format_exc()}
        sys.stdout.write(json.dumps(result) + "\\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
"""


def _ensure_wine_environment() -> None:
    """Auto-fix python39._pth in Wine and repair agent.py on macOS if needed."""
    if sys.platform != "darwin":
        return
    try:
        wineprefix = os.environ.get(
            "WINEPREFIX",
            os.path.expanduser("~/Library/Application Support/net.metaquotes.wine.metatrader5"),
        )
        wine_pth = os.path.join(wineprefix, "drive_c", "Python39", "python39._pth")
        if os.path.exists(wine_pth):
            with open(wine_pth, "r", encoding="utf-8") as f:
                content = f.read()
            if "Lib\\site-packages" not in content and "Lib/site-packages" not in content:
                with open(wine_pth, "w", encoding="utf-8") as f:
                    f.write("python39.zip\n.\nLib\\site-packages\nimport site\n")
                logger.info("wine_python_pth_auto_repaired", path=wine_pth)
    except Exception as e:
        logger.debug("wine_python_pth_check_skipped", error=str(e))

    try:
        import importlib.util
        spec = importlib.util.find_spec("mt5_mac")
        if spec and spec.origin:
            agent_path = os.path.join(os.path.dirname(spec.origin), "agent.py")
            if os.path.exists(agent_path):
                with open(agent_path, "r", encoding="utf-8") as f:
                    agent_src = f.read()
                needs_repair = False
                try:
                    compile(agent_src, agent_path, "exec")
                except SyntaxError:
                    needs_repair = True
                if needs_repair or 'action == "terminal_info"' not in agent_src:
                    with open(agent_path, "w", encoding="utf-8") as f:
                        f.write(CLEAN_AGENT_CODE)
                    logger.info("wine_agent_repaired", path=agent_path)
    except Exception as e:
        logger.debug("wine_agent_check_skipped", error=str(e))


# Import MT5 library (native MetaTrader5 on Windows, mt5_mac bridge on macOS Apple Silicon)
if sys.platform == "win32":
    try:
        import MetaTrader5 as mt5  # type: ignore[import-not-found] # pyright: ignore[reportMissingImports]
    except ImportError:
        mt5 = None
else:
    try:
        import mt5_mac as mt5  # type: ignore[import-not-found] # pyright: ignore[reportMissingImports]
    except ImportError:
        try:
            import MetaTrader5 as mt5  # type: ignore[import-not-found] # pyright: ignore[reportMissingImports]
        except ImportError:
            mt5 = None


class MT5Client:
    """Manages connection to the MetaTrader 5 terminal."""

    def __init__(self) -> None:
        self._connected = False
        self._lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def run_in_thread(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Execute a synchronous MT5 call in a thread pool."""
        return await asyncio.to_thread(func, *args, **kwargs)

    async def connect(self) -> bool:
        """Initialize connection to MT5 terminal."""
        if mt5 is None:
            logger.error("mt5_package_missing", msg="Neither MetaTrader5 nor mt5_mac is installed")
            return False

        async with self._lock:
            if self._connected:
                try:
                    info = await self.run_in_thread(mt5.terminal_info)
                    if info and getattr(info, "connected", False):
                        return True
                except Exception:
                    self._connected = False

            logger.info("mt5_connecting", server=settings.mt5.server, login=settings.mt5.login)

            def _init() -> bool:
                _ensure_wine_environment()
                init_kwargs: dict[str, Any] = {"portable": True}
                if settings.mt5.path:
                    init_kwargs["path"] = settings.mt5.path
                if settings.mt5.login:
                    init_kwargs["login"] = settings.mt5.login
                if settings.mt5.password:
                    init_kwargs["password"] = settings.mt5.password
                if settings.mt5.server:
                    init_kwargs["server"] = settings.mt5.server

                try:
                    # Cleanly reset previous backend process if lingering
                    if hasattr(mt5, "shutdown"):
                        try:
                            mt5.shutdown()
                        except Exception:
                            pass
                    ok = mt5.initialize(**init_kwargs)
                    if not ok:
                        # Retry without path if path failed
                        if "path" in init_kwargs:
                            init_kwargs.pop("path")
                            ok = mt5.initialize(**init_kwargs)
                    return ok
                except Exception as e:
                    logger.error("mt5_initialize_exception", error=str(e))
                    return False

            ok = await self.run_in_thread(_init)

            if not ok:
                err = mt5.last_error() if hasattr(mt5, "last_error") else None
                logger.error("mt5_connect_failed", error=err)
                self._connected = False
                return False

            self._connected = True
            terminal = await self.run_in_thread(mt5.terminal_info)
            account = await self.run_in_thread(mt5.account_info)

            logger.info(
                "mt5_connected",
                terminal_name=getattr(terminal, "name", "MT5"),
                account_login=getattr(account, "login", None),
                balance=getattr(account, "balance", None),
            )
            return True

    async def disconnect(self) -> None:
        """Shut down MT5 connection."""
        async with self._lock:
            if self._connected and mt5:
                await self.run_in_thread(mt5.shutdown)
                self._connected = False
                logger.info("mt5_disconnected")

    async def ping(self) -> bool:
        """Check if terminal is responsive and connected."""
        if not self._connected or mt5 is None:
            return False

        try:
            info = await self.run_in_thread(mt5.terminal_info)
            connected = bool(info and getattr(info, "connected", False))
            if not connected:
                self._connected = False
            return connected
        except Exception:
            self._connected = False
            return False


# Singleton MT5 client instance
mt5_client = MT5Client()
