"""Structured logging via ``structlog``.

JSON output is used in ``prod`` and a colored console renderer in
``dev``.  Event names are ``snake_case``; prefer one positive event per
meaningful step so a silent outage is detectable by the **absence** of
an expected event, not only by the presence of errors.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from .config import get_settings


def configure_logging() -> None:
    """Configure ``structlog`` and the standard library root logger.

    Should be called once at application startup, before any logger is used.
    The log level and renderer are derived from :func:`get_settings`.
    """
    settings = get_settings()
    level = getattr(logging, settings.log_level)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.env == "prod":
        renderer: Any = structlog.processors.JSONRenderer()
        shared_processors.append(structlog.processors.format_exc_info)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*shared_processors, renderer],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structured logger bound to ``name``.

    Args:
        name: Logger name. Conventionally the dotted module path.

    Returns:
        A :class:`structlog.stdlib.BoundLogger` ready to use.
    """
    return structlog.get_logger(name)
