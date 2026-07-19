"""Loguru-based structured logging configuration.

All logs are written to server.log as JSON lines for full traceability.
Stdlib logging is intercepted and funneled to loguru.
Context vars (request_id, node_id, chat_id) from contextualize() are
included at top level for easy grep/filter.

Every bound or kwarg-style ``extra`` field (e.g.
``logger.warning("event", latency_s=1.2)``) is emitted at top level of
the JSON line so chain-walk telemetry survives into server.log
(SP-PROXY-LOG-EXTRA). An extra whose name collides with one of the six
fixed record fields is re-emitted under an ``extra_`` prefix instead of
overwriting it; non-JSON-serialisable values degrade to ``str``.
"""

import json
import logging
from pathlib import Path

from loguru import logger

_configured = False

_CONTEXT_KEYS = ("request_id", "node_id", "chat_id")
"""Context keys promoted to top-level JSON for traceability."""

_RESERVED_KEYS = ("time", "level", "message", "module", "function", "line")
"""Fixed record fields; a colliding extra is re-emitted as ``extra_<key>``."""


def _serialize_with_context(record) -> str:
    """Format record as JSON with context vars and extras at top level.

    Builds the six fixed fields, promotes the :data:`_CONTEXT_KEYS`,
    then merges every remaining ``record["extra"]`` item into the
    output object — a key colliding with a fixed field lands under an
    ``extra_`` prefix so the record fields are never overwritten.
    Returns a format template; we inject _json into record for output.
    """
    extra = record.get("extra", {})
    out = {
        "time": str(record["time"]),
        "level": record["level"].name,
        "message": record["message"],
        "module": record["name"],
        "function": record["function"],
        "line": record["line"],
    }
    for key in _CONTEXT_KEYS:
        if key in extra and extra[key] is not None:
            out[key] = extra[key]
    for key, value in extra.items():
        if key in _CONTEXT_KEYS:
            continue
        if key in _RESERVED_KEYS:
            out[f"extra_{key}"] = value
        else:
            out[key] = value
    record["_json"] = json.dumps(out, default=str)
    return "{_json}\n"


class InterceptHandler(logging.Handler):
    """Redirect stdlib logging to loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = logging.currentframe(), 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def configure_logging(log_file: str, *, force: bool = False) -> None:
    """Configure loguru with JSON output to log_file and intercept stdlib logging.

    Idempotent: skips if already configured (e.g. hot reload).
    Use ``force=True`` to reconfigure (e.g. in tests with a
    different log path).

    The startup sequence drops the default loguru stderr handler,
    ensures the log directory exists, installs a JSON-lines file sink
    at DEBUG level (append mode, 50 MB rotation, 30-day retention so NIM
    telemetry survives restarts and accumulates for later analysis, with
    context vars promoted to top-level keys via :data:`_CONTEXT_KEYS`)
    and routes every root stdlib logger record to loguru via
    :class:`InterceptHandler`.
    """
    global _configured
    if _configured and not force:
        return
    _configured = True

    logger.remove()

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    logger.add(
        log_file,
        level="DEBUG",
        format=_serialize_with_context,
        encoding="utf-8",
        mode="a",
        rotation="50 MB",
        retention="30 days",
    )

    intercept = InterceptHandler()
    logging.root.handlers = [intercept]
    logging.root.setLevel(logging.DEBUG)
