"""
Application settings — loaded from environment variables / .env file.

All settings are type-safe via Pydantic.  Trading defaults here are fallbacks;
runtime values live in the ``bot_config`` database table and can be changed
dynamically through the Telegram admin console.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """PostgreSQL connection."""

    model_config = SettingsConfigDict(
        env_prefix="DATABASE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    url: str = "postgresql+asyncpg://trading:trading@localhost:5432/trading_bot"

    # Synchronous URL for Alembic migrations
    @property
    def sync_url(self) -> str:
        return self.url.replace("+asyncpg", "")


class TelegramSettings(BaseSettings):
    """Telegram bot & userbot credentials."""

    model_config = SettingsConfigDict(
        env_prefix="TELEGRAM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Personal Bot (python-telegram-bot)
    bot_token: str = ""
    admin_chat_id: int = 0

    # Signal Listener (Telethon userbot)
    api_id: int = 0
    api_hash: str = ""
    signal_chat_id: int = 0


class MT5Settings(BaseSettings):
    """MetaTrader 5 terminal connection."""

    model_config = SettingsConfigDict(
        env_prefix="MT5_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    path: str = ""
    login: int = 0
    password: str = ""
    server: str = ""
    timeout: int = 10000
    broker_utc_offset_hours: int = 3


class TradingSettings(BaseSettings):
    """Default trading parameters — overridden by DB config at runtime."""

    model_config = SettingsConfigDict(
        env_prefix="TRADING_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = True
    max_open_positions: int = 5
    max_lot: float = 0.05
    min_lot: float = 0.01
    default_lot: float = 0.01
    max_daily_loss_percent: float = 10.0
    require_sl: bool = True
    require_tp: bool = False
    allowed_symbols: list[str] | str = Field(default_factory=lambda: ["XAUUSD"])
    signal_expire_seconds: int = 300
    duplicate_timeout_seconds: int = 600

    # Dual Entry Setup Rules
    enable_dual_entry: bool = True
    lot_min_entry: float = 0.01
    lot_max_entry: float = 0.01
    max_slippage_entry: float = 1.0

    @field_validator("allowed_symbols", mode="after")
    @classmethod
    def parse_allowed_symbols(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            return [s.strip().upper() for s in v.split(",") if s.strip()]
        return v


class AppSettings(BaseSettings):
    """Root application settings — aggregates all sub-settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    debug: bool = False
    log_level: str = "ERROR"

    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    mt5: MT5Settings = Field(default_factory=MT5Settings)
    trading: TradingSettings = Field(default_factory=TradingSettings)


# Singleton — import this everywhere
settings = AppSettings()
