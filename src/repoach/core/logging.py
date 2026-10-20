"""Structured logging via ``structlog``.

JSON output is used in ``prod`` and a colored console renderer in
``dev``.  Event names are ``snake_case``; prefer one positive event per
meaningful step so a silent outage is detectable by the **absence** of
an expected event, not only by the presence of errors.

Logs always go to **stderr** (SP-CLI-STDOUT-ONE-JSON): a command's
stdout carries its result alone, so ``repoach review pr … | jq`` parses.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from .config import get_settings


def _stderr_print_logger(*_args: Any) -> structlog.PrintLogger:
    """Return a print logger bound to the ``sys.stderr`` current at creation.

    Resolved per logger rather than once, so a stream swapped after
    import (a test runner capturing output) is honoured.
    """
    return structlog.PrintLogger(file=sys.stderr)


def route_default_logs_to_stderr() -> None:
    """Point structlog's default print logger at stderr instead of stdout.

    structlog's unconfigured default prints to stdout; this changes only
    the logger factory, so a command that never reaches
    :func:`configure_logging` (a settings-free command, a subcommand
    invoked directly) still keeps its stdout for its result.
    """
    structlog.configure(logger_factory=_stderr_print_logger)


def configure_logging() -> None:
    """Configure ``structlog`` and the standard library root logger.

    Should be called once at application startup, before any logger is used.
    The log level and renderer are derived from :func:`get_settings`; the
    stream is stderr.
    """
    settings = get_settings()
    level = getattr(logging, settings.log_level)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
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
