"""API layer for Claude Code Proxy.

The FastAPI ``app`` and ``create_app`` symbols intentionally live in
:mod:`ferova.llm_proxy.api.app` and are NOT re-exported here.
Re-exporting them used to force :mod:`api.app` to load every time a
sibling module (eg. :mod:`api.models.agent_v1`) was imported, and
:mod:`api.app` runs :func:`configure_logging` at module level —
which swaps ``logging.root.handlers`` for the loguru
:class:`InterceptHandler` and silently reroutes every stdlib log
record into the proxy's ``server.log``.  That hijack hid scheduler
/ sentinel / webhook logs for hours.  See SP-SCHEDULER-LOG-ROUTE.
Import the FastAPI app directly via
``from ferova.llm_proxy.api.app import app, create_app`` when
you actually need it.
"""

from ._failover import PeekResult, peek_for_content
from .models import (
    MessagesRequest,
    MessagesResponse,
    TokenCountRequest,
    TokenCountResponse,
)

__all__ = [
    "MessagesRequest",
    "MessagesResponse",
    "PeekResult",
    "TokenCountRequest",
    "TokenCountResponse",
    "peek_for_content",
]
