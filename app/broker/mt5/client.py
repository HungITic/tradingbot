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


def _ensure_wine_environment() -> None:
    """Auto-fix python39._pth in Wine on macOS Apple Silicon if needed."""
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
