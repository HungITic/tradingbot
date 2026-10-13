"""
Trading Bot V1 — Main Application Entrypoint.

Initializes logging, database connections, MT5 adapter,
Telegram personal bot, and signal listener with graceful shutdown handling.
"""

from __future__ import annotations

import os
import sys

# Prevent app/ directory from shadowing standard library modules (e.g. signal)
_app_dir = os.path.dirname(os.path.abspath(__file__))
_root_dir = os.path.dirname(_app_dir)
if sys.path and os.path.abspath(sys.path[0]) == _app_dir:
    sys.path.pop(0)
if _root_dir not in sys.path:
    sys.path.insert(0, _root_dir)

import asyncio
import signal

import structlog

from app.config.database import close_db, init_db
from app.config.settings import settings
from app.core.logging import setup_logging
from app.telegram.personal_bot import personal_bot
from app.telegram.signal_listener import signal_listener
from app.trading.service import trading_service

logger = structlog.get_logger(__name__)


class Application:
    """Trading Bot Lifecycle Manager."""

    def __init__(self) -> None:
        self._running = False
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """Start all services."""
        self._running = True
        logger.info(
            "app_starting",
            debug=settings.debug,
            allowed_symbols=settings.trading.allowed_symbols,
        )

        try:
            # 1. Database
            logger.info("initializing_database")
            await init_db()
            logger.info("database_ready")

            # 1.1 Verify and seed initial configuration & rules into DB
            from app.config.seeder import seed_initial_data
            seed_res = await seed_initial_data()
            logger.info("database_initial_data_verified", **seed_res)

            # 1.2 Load Risk Config and Management Rules from DB
            await trading_service.load_risk_config_from_db()
            rule_count = await trading_service.load_management_rules_from_db()
            logger.info("db_config_and_rules_loaded", rule_count=rule_count)

            # 2. MT5 Broker Connection
            logger.info("connecting_mt5_broker")
            mt5_ok = await trading_service.broker.connect()
            if mt5_ok:
                logger.info("mt5_broker_connected")
                # Auto-detect broker symbol suffix (e.g. Exness 'm')
                try:
                    resolved_gold = await trading_service.broker.resolve_symbol("XAUUSD")
                    if resolved_gold and resolved_gold != "XAUUSD" and resolved_gold.upper().startswith("XAUUSD"):
                        suffix = resolved_gold[len("XAUUSD"):]
                        trading_service.parser.normalizer.set_broker_suffix(suffix)
                        logger.info("broker_symbol_suffix_detected", suffix=suffix, resolved_symbol=resolved_gold)
                except Exception as e:
                    logger.warning("broker_suffix_detection_failed", error=str(e))
            else:
                logger.warning("mt5_broker_connect_failed", msg="Check if MT5 terminal is open.")

            # 3. Personal Telegram Bot (Control Plane)
            if settings.telegram.bot_token:
                logger.info("starting_personal_bot")
                await personal_bot.start()
                logger.info("personal_bot_ready")
            else:
                logger.warning("personal_bot_skipped", msg="TELEGRAM_BOT_TOKEN not configured.")

            # 4. Telegram Signal Listener (Userbot)
            if settings.telegram.api_id and settings.telegram.api_hash:
                logger.info("starting_signal_listener")
                await signal_listener.start()
                logger.info("signal_listener_ready")
            else:
                logger.warning("signal_listener_skipped", msg="Telethon credentials not configured.")

            # 5. Start Background Jobs
            trading_service.start_sync_job()
            logger.info("background_sync_job_started")

            # Send startup broadcast notification to admin
            await trading_service.notify(
                "🚀 <b>Trading Bot V1 Started Successfully!</b>\n\n"
                f"• <b>MT5 Status:</b> {'✅ Connected' if mt5_ok else '⚠️ Disconnected'}\n"
                f"• <b>Signal Listener:</b> {'✅ Active' if signal_listener._running else '⚠️ Inactive'}\n"
                f"• <b>Risk Status:</b> {'🟢 Bot Active' if trading_service.risk_config.enabled else '🔴 Bot Paused'}\n\n"
                "Send /status or /help to interact."
            )

            # Keep running until shutdown signal
            logger.info("app_running", status="READY")
            await self._stop_event.wait()

        except asyncio.CancelledError:
            logger.info("app_cancelled")
        except Exception as e:
            logger.exception("app_fatal_error", error=str(e))
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Gracefully shut down all services."""
        if not self._running:
            return
        self._running = False
        logger.info("app_shutting_down")

        # Notify admin of shutdown
        try:
            await trading_service.notify("🛑 <b>Trading Bot V1 is shutting down...</b>")
        except Exception:
            pass

        # 1. Stop Signal Listener
        try:
            await signal_listener.stop()
        except Exception as e:
            logger.warning("error_stopping_signal_listener", error=str(e))

        # 2. Stop Personal Bot
        try:
            await personal_bot.stop()
        except Exception as e:
            logger.warning("error_stopping_personal_bot", error=str(e))

        # 3. Disconnect Broker
        try:
            await trading_service.broker.disconnect()
        except Exception as e:
            logger.warning("error_disconnecting_broker", error=str(e))

        # 4. Close DB
        try:
            await close_db()
        except Exception as e:
            logger.warning("error_closing_db", error=str(e))

        logger.info("app_stopped")

    def trigger_shutdown(self) -> None:
        """Signal the app to stop."""
        self._stop_event.set()


def main() -> None:
    """Synchronous process entrypoint."""
    setup_logging()
    app = Application()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, app.trigger_shutdown)

    try:
        loop.run_until_complete(app.start())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
