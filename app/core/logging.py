"""
Error-only structured logs with three-day file retention.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import structlog

from app.core.log_files import ErrorFileHandler, LOG_DIRECTORY


def setup_logging(log_directory: Path = LOG_DIRECTORY) -> None:
    """Configure standard library and structlog processors."""
    log_level = logging.ERROR

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

    renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[structlog.stdlib.filter_by_level] + shared_processors + [
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

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(log_level)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    for previous in root_logger.handlers[:]:
        root_logger.removeHandler(previous)
        previous.close()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)
    # The daemon's outer process owns files and captures pre-startup tracebacks.
    # Direct `python run.py` writes the same managed file itself.
    if os.environ.get('BOT_LOG_EXTERNAL') != '1':
        file_handler = ErrorFileHandler(log_directory)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    # Silence noisy third-party loggers
    for logger_name in ("httpx", "httpcore", "telethon", "telegram", "sqlalchemy"):
        logging.getLogger(logger_name).setLevel(log_level)
    # Remove earlier library-specific thresholds/handlers: real network errors
    # must still be visible, while child INFO/WARNING records stay filtered.
    for name, child in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(child, logging.Logger) and name.startswith(('sqlalchemy.', 'telethon.', 'telegram.', 'httpx.', 'httpcore.')):
            child.handlers.clear()
            child.setLevel(log_level)
            child.propagate = True
    logging.captureWarnings(True)
