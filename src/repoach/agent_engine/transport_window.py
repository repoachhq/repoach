"""The window inside which one model call retries a transport error.

SP-REVIEW-SURVIVES-TRANSPORT-FAILURES G1. On 2026-11-04 the runner host
lost its network for about 100 s (``Runner connect error: The HTTP
request timed out after 00:01:40``, at 21:38, 21:40 and 22:00 UTC); the
proxy's breaker answered every call of that minute with a 502 and the
review of #288 ended incomplete, exit 3, waiting for a person to re-run
it. :class:`TransportWindow` gives each model call a total time within
which a transport error is retried with exponential backoff, so an
outage shorter than the window costs time, not the review.

The window is counted once per call, from its first attempt: an outer
retry loop that sees :func:`window_spent` on the error does not open a
second window (:func:`~repoach.review.retry_backoff.retry_with_backoff`,
``AgentLoop._call_turn_with_retry``). Only a :class:`GatewayTransportError`
whose status is not a 4xx is retried; every other error, and a 408 or
429, is raised at once, exactly as before.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..providers.errors import GatewayTransportError

if TYPE_CHECKING:
    from ..providers.select import ProviderRoute

FIRST_BACKOFF_S = 5.0
"""Seconds before the second attempt (``transport_backoff_first_s``)."""

BACKOFF_FACTOR = 2.0
"""Each later wait is the previous one times this (``transport_backoff_factor``)."""

BACKOFF_CAP_S = 60.0
"""No single wait is longer than this (``transport_backoff_cap_s``)."""

_SPENT_ATTRIBUTE = "transport_window_spent"


def retryable_transport(exc: BaseException) -> bool:
    """Return True when the window may retry ``exc``: a transport error that is not a 4xx."""
    if not isinstance(exc, GatewayTransportError):
        return False
    status = exc.status
    return status is None or not 400 <= status < 500


def window_spent(exc: BaseException) -> bool:
    """Return True when ``exc`` ended a call whose transport window was used up."""
    return getattr(exc, _SPENT_ATTRIBUTE, False) is True


def mark_window_spent(exc: BaseException) -> None:
    """Record on ``exc`` that its call's window is used up, so no outer loop opens another."""
    setattr(exc, _SPENT_ATTRIBUTE, True)


def configured_transport_window_s(route: ProviderRoute) -> float:
    """Return the window of a provider route: ``[provider].transport_retry_window_s``."""
    return float(route.section.transport_retry_window_s)


def backoff_s(attempt: int) -> float:
    """Return the wait after failed attempt ``attempt`` (1-based), before the cap of the window."""
    return min(BACKOFF_CAP_S, FIRST_BACKOFF_S * BACKOFF_FACTOR ** (attempt - 1))


class TransportWindow:
    """The retry budget of one model call.

    Attributes:
        window_s: Total seconds the call may spend retrying, from its
            first attempt; 0 disables the window.
        started: The clock reading at the first attempt.
    """

    def __init__(self, window_s: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.window_s = window_s
        self._clock = clock
        self.started = clock()

    def elapsed_s(self) -> float:
        """Return the seconds since the first attempt."""
        return self._clock() - self.started

    def next_wait(self, exc: BaseException, attempt: int) -> float | None:
        """Return how long to wait before retrying after ``exc``, or None to give up.

        None when the window is disabled, when ``exc`` is not retryable
        (:func:`retryable_transport`), or when no time is left. The last
        wait is cut to the time left, so the final attempt lands at the
        edge of the window rather than past it. An error carrying
        ``retry_after_s`` (the proxy's ``Retry-After`` for a tripped
        chain) is waited out exactly when it ends inside the window, and
        given up at once when it does not: a 410 tripped for 7 days is
        never retried for 300 s.

        Args:
            exc: The error the attempt raised.
            attempt: The 1-based number of the attempt that raised it.
        """
        if self.window_s <= 0 or not retryable_transport(exc):
            return None
        remaining = self.window_s - self.elapsed_s()
        if remaining <= 0:
            return None
        retry_after = getattr(exc, "retry_after_s", None)
        if isinstance(retry_after, (int, float)) and retry_after > 0:
            return float(retry_after) if retry_after <= remaining else None
        return min(backoff_s(attempt), remaining)


__all__ = [
    "BACKOFF_CAP_S",
    "BACKOFF_FACTOR",
    "FIRST_BACKOFF_S",
    "TransportWindow",
    "backoff_s",
    "configured_transport_window_s",
    "mark_window_spent",
    "retryable_transport",
    "window_spent",
]
