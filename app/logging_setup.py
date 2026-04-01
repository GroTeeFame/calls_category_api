from __future__ import annotations

"""Logging bootstrap for console + rotating file output."""

import logging
import os
from logging.handlers import RotatingFileHandler

from app.config import Settings


_LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_configured = False


def _resolve_log_level(level_name: str) -> int:
    """Resolve a string log level into a `logging` numeric constant."""
    cleaned_level = level_name.split("#", 1)[0].strip().split()[0] if level_name.strip() else ""
    return getattr(logging, cleaned_level.upper(), logging.DEBUG)


def configure_logging(settings: Settings) -> None:
    """Configure process-wide logging handlers once.

    Creates both:
    - stream handler for terminal output
    - rotating file handler for persistent logs
    """
    global _configured
    if _configured:
        return

    log_level = _resolve_log_level(settings.log_level)
    log_file_path = settings.log_file_path
    log_file_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(_LOG_FORMAT, _DATE_FORMAT)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(log_level)
    stream_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        filename=log_file_path,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    try:
        os.chmod(log_file_path, 0o600)
    except OSError:
        # Best-effort hardening; do not crash logging setup on permission mismatch.
        pass
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)

    app_logger = logging.getLogger("calls_category_api")
    app_logger.handlers.clear()
    app_logger.setLevel(log_level)
    app_logger.addHandler(stream_handler)
    app_logger.addHandler(file_handler)
    app_logger.propagate = False

    _configured = True
    app_logger.info(
        "Logging configured level=%s file=%s max_bytes=%s backup_count=%s",
        settings.log_level,
        log_file_path,
        settings.log_max_bytes,
        settings.log_backup_count,
    )
