"""
MT5 connection client wrapper.

Handles initializing connection to MT5 terminal, authentication,
and threadpool offloading so synchronous MT5 IPC calls don't block asyncio.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import structlog

import sys

from app.config.settings import settings

logger = structlog.get_logger(__name__)

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
                return True

            logger.info("mt5_connecting", server=settings.mt5.server, login=settings.mt5.login)

            def _init() -> bool:
                init_kwargs: dict[str, Any] = {"portable": True}
                if settings.mt5.path:
                    init_kwargs["path"] = settings.mt5.path
                if settings.mt5.login:
                    init_kwargs["login"] = settings.mt5.login
                if settings.mt5.password:
                    init_kwargs["password"] = settings.mt5.password
                if settings.mt5.server:
                    init_kwargs["server"] = settings.mt5.server

                ok = mt5.initialize(**init_kwargs)
                if not ok:
                    # Retry without path if path failed
                    if "path" in init_kwargs:
                        init_kwargs.pop("path")
                        ok = mt5.initialize(**init_kwargs)
                return ok

            ok = await self.run_in_thread(_init)

            if not ok:
                err = mt5.last_error()
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

        info = await self.run_in_thread(mt5.terminal_info)
        return bool(info and getattr(info, "connected", False))


# Singleton MT5 client instance
mt5_client = MT5Client()
