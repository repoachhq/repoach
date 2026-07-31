"""Shared retry-with-backoff loop mechanics.

Extracted from :class:`~repoach.review.reviewer.Reviewer` and
:class:`~repoach.review.reviewer.Developer`, which each hand-rolled the
same schedule-a-wait / attempt / catch-and-retry-on-exception /
catch-and-retry-on-rejected-outcome / exhaustion-fallback loop over
their own ``_RETRY_BACKOFFS_S`` class attribute. This module holds
the one copy of that loop; callers supply only their per-attempt
closure (what to call and whether the result is acceptable) and shape
the returned :class:`RetryResult` into their own return contract.

SP-RETRY-BACKOFF-DEDUP.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from ..core.logging import get_logger

T = TypeVar("T")

_log = get_logger(__name__)


@dataclass(frozen=True)
class AttemptOutcome(Generic[T]):
    """One attempt's result, as judged by the caller's accept-predicate.

    Attributes:
        value: The value produced by the attempt, regardless of
            whether it is accepted.
        accept: Whether the caller considers ``value`` good enough to
            stop retrying.
    """

    value: T
    accept: bool


@dataclass(frozen=True)
class RetryResult(Generic[T]):
    """Outcome of a full :func:`retry_with_backoff` run.

    Attributes:
        value: The accepted value on success, the last-seen rejected
            value when the schedule exhausts without acceptance, or
            ``None`` when every attempt raised (or the schedule was
            empty).
        error: The last exception raised by an attempt, or ``None``
            when at least one attempt returned a value (accepted or
            not) or the schedule was empty.
        attempts: Number of attempts actually made.
        accepted: Whether ``value`` was produced by an accepted
            attempt.
    """

    value: T | None
    error: Exception | None
    attempts: int
    accepted: bool


def retry_with_backoff(
    attempt: Callable[[int], AttemptOutcome[T]],
    *,
    backoffs: tuple[float, ...],
    log_scope: str,
    log_context: Mapping[str, Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> RetryResult[T]:
    """Run ``attempt`` up to ``len(backoffs)`` times with backoff waits.

    Before each attempt after the first, sleeps for the scheduled
    number of seconds. An attempt that raises is caught and retried.
    An attempt that returns an :class:`AttemptOutcome` with
    ``accept=False`` is also retried, but its value is remembered as
    the fallback if the schedule exhausts without an accepted outcome.
    The first accepted outcome stops the loop immediately.

    Args:
        attempt: Callable invoked with the 1-based attempt number,
            returning an :class:`AttemptOutcome`.
        backoffs: Wait time in seconds before each attempt; the first
            entry is conventionally ``0.0`` (no wait before the first
            attempt). An empty tuple makes zero attempts.
        log_scope: Prefix for the structured log events this function
            emits (``f"{log_scope}.retry_wait"``,
            ``f"{log_scope}.attempt_failed"``,
            ``f"{log_scope}.rejected_retry"``,
            ``f"{log_scope}.exhausted_rejected"``,
            ``f"{log_scope}.exhausted_exception"``).
        log_context: Extra key/value pairs merged into every log call
            (e.g. ``role``, ``pr_number``, ``spec_id``).
        sleep: Injectable sleep callable, defaults to :func:`time.sleep`.

    Returns:
        A :class:`RetryResult` describing the outcome.
    """
    context = dict(log_context) if log_context else {}
    last_error: Exception | None = None
    last_outcome: AttemptOutcome[T] | None = None
    attempts_made = 0

    for attempt_no, wait_s in enumerate(backoffs, start=1):
        attempts_made = attempt_no
        if wait_s > 0:
            _log.info(
                f"{log_scope}.retry_wait",
                attempt=attempt_no,
                wait_s=wait_s,
                last_error=type(last_error).__name__ if last_error else None,
                **context,
            )
            sleep(wait_s)
        try:
            outcome = attempt(attempt_no)
        except Exception as exc:
            last_error = exc
            _log.warning(
                f"{log_scope}.attempt_failed",
                attempt=attempt_no,
                of=len(backoffs),
                error=type(exc).__name__,
                message=str(exc)[:200],
                **context,
            )
            continue

        last_outcome = outcome
        if outcome.accept:
            return RetryResult(
                value=outcome.value,
                error=None,
                attempts=attempt_no,
                accepted=True,
            )
        _log.warning(
            f"{log_scope}.rejected_retry",
            attempt=attempt_no,
            of=len(backoffs),
            **context,
        )

    if last_outcome is not None:
        _log.error(
            f"{log_scope}.exhausted_rejected",
            attempts=attempts_made,
            **context,
        )
        return RetryResult(
            value=last_outcome.value,
            error=None,
            attempts=attempts_made,
            accepted=False,
        )

    if attempts_made == 0:
        return RetryResult(value=None, error=None, attempts=0, accepted=False)

    _log.error(
        f"{log_scope}.exhausted_exception",
        attempts=attempts_made,
        final_error=type(last_error).__name__ if last_error else "Unknown",
        **context,
    )
    return RetryResult(
        value=None,
        error=last_error,
        attempts=attempts_made,
        accepted=False,
    )
