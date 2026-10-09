"""
Structured logging configuration using structlog.
Outputs colored logs in development and structured JSON in production.
"""

from __future__ import annotations

import logging
import sys

import structlog

from app.config.settings import settings


def setup_logging() -> None:
    """Configure standard library and structlog processors."""
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    if settings.debug:
        renderer = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)

    # Silence noisy third-party loggers
    for logger_name in ("httpx", "httpcore", "telethon", "telegram"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    # Specifically suppress noisy network polling errors from telegram and telethon
    logging.getLogger("telegram.ext.Updater").setLevel(logging.CRITICAL)
    logging.getLogger("telethon.network.mtprotosender").setLevel(logging.CRITICAL)
