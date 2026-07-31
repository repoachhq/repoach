"""Unit tests for SP-RETRY-BACKOFF-DEDUP's shared retry-with-backoff helper.

Covers :func:`repoach.review.retry_backoff.retry_with_backoff` directly
(loop mechanics: acceptance, exception retry, rejected-outcome retry,
both exhaustion shapes, the empty-backoffs edge case, and the
scheduled-sleep contract) plus one discriminating test proving both
:class:`~repoach.review.reviewer.Reviewer` and
:class:`~repoach.review.reviewer.Developer` route their retry paths
through this single shared function rather than through independent
copies of the loop.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from repoach.review import reviewer as reviewer_module
from repoach.review.retry_backoff import (
    AttemptOutcome,
    RetryResult,
    retry_with_backoff,
)


def test_accepts_first_attempt_no_retry() -> None:
    sleep = MagicMock()
    attempt = MagicMock(return_value=AttemptOutcome(value="first", accept=True))

    result = retry_with_backoff(
        attempt,
        backoffs=(0.0, 30.0, 90.0),
        log_scope="test.scope",
        sleep=sleep,
    )

    assert attempt.call_count == 1
    sleep.assert_not_called()
    assert result == RetryResult(value="first", error=None, attempts=1, accepted=True)


def test_retries_after_exception_then_succeeds() -> None:
    sleep = MagicMock()
    attempt = MagicMock(
        side_effect=[RuntimeError("flake"), AttemptOutcome(value="second", accept=True)],
    )

    result = retry_with_backoff(
        attempt,
        backoffs=(0.0, 30.0, 90.0),
        log_scope="test.scope",
        sleep=sleep,
    )

    assert attempt.call_count == 2
    sleep.assert_called_once_with(30.0)
    assert result == RetryResult(value="second", error=None, attempts=2, accepted=True)


def test_retries_after_rejected_outcome_then_succeeds() -> None:
    sleep = MagicMock()
    attempt = MagicMock(
        side_effect=[
            AttemptOutcome(value="rejected", accept=False),
            AttemptOutcome(value="accepted", accept=True),
        ],
    )

    result = retry_with_backoff(
        attempt,
        backoffs=(0.0, 30.0, 90.0),
        log_scope="test.scope",
        sleep=sleep,
    )

    assert attempt.call_count == 2
    sleep.assert_called_once_with(30.0)
    assert result == RetryResult(value="accepted", error=None, attempts=2, accepted=True)


def test_exhausted_rejected_returns_last_value_not_error() -> None:
    sleep = MagicMock()
    attempt = MagicMock(
        side_effect=[
            AttemptOutcome(value="one", accept=False),
            AttemptOutcome(value="two", accept=False),
            AttemptOutcome(value="three", accept=False),
        ],
    )

    result = retry_with_backoff(
        attempt,
        backoffs=(0.0, 30.0, 90.0),
        log_scope="test.scope",
        sleep=sleep,
    )

    assert attempt.call_count == 3
    assert result == RetryResult(value="three", error=None, attempts=3, accepted=False)


def test_exhausted_exceptions_returns_last_error_and_none_value() -> None:
    sleep = MagicMock()
    last_error = RuntimeError("third-flake")
    attempt = MagicMock(
        side_effect=[RuntimeError("first-flake"), RuntimeError("second-flake"), last_error],
    )

    result = retry_with_backoff(
        attempt,
        backoffs=(0.0, 30.0, 90.0),
        log_scope="test.scope",
        sleep=sleep,
    )

    assert attempt.call_count == 3
    assert result.value is None
    assert result.error is last_error
    assert result.attempts == 3
    assert result.accepted is False


def test_empty_backoffs_makes_zero_attempts() -> None:
    sleep = MagicMock()
    attempt = MagicMock()

    result = retry_with_backoff(
        attempt,
        backoffs=(),
        log_scope="test.scope",
        sleep=sleep,
    )

    attempt.assert_not_called()
    sleep.assert_not_called()
    assert result == RetryResult(value=None, error=None, attempts=0, accepted=False)


def test_sleep_invoked_with_scheduled_backoff_seconds_only() -> None:
    sleep = MagicMock()
    attempt = MagicMock(
        side_effect=[
            RuntimeError("flake-1"),
            RuntimeError("flake-2"),
            AttemptOutcome(value="third", accept=True),
        ],
    )

    result = retry_with_backoff(
        attempt,
        backoffs=(0.0, 5.0, 10.0),
        log_scope="test.scope",
        sleep=sleep,
    )

    assert sleep.call_args_list == [((5.0,),), ((10.0,),)]
    assert result.attempts == 3
    assert result.accepted is True


class TestBothCallersRouteThroughSharedHelper:
    """Discriminator: fails on old code where each caller has its own loop.

    Patches ``reviewer.retry_with_backoff`` (the shared symbol imported
    into ``reviewer.py``) and drives both
    :class:`~repoach.review.reviewer.Architect` (representative
    :class:`Reviewer` subclass) and
    :class:`~repoach.review.reviewer.Developer` through their public
    entry points, asserting the shared helper is invoked by both.
    """

    def test_reviewer_call_with_retry_invokes_shared_helper(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        loop = MagicMock()
        loop.run_oneshot.return_value = MagicMock(
            text='{"verdict": "APPROVE", "summary": "ok", "comments": []}',
        )
        reviewer = reviewer_module.Architect(loop=loop)

        spy = MagicMock(wraps=reviewer_module.retry_with_backoff)
        monkeypatch.setattr(reviewer_module, "retry_with_backoff", spy)

        reviewer._call_with_retry("prompt", pr_number=1)

        spy.assert_called_once()
        assert spy.call_args.kwargs["log_scope"] == "review.bot"

    def test_developer_call_with_retry_invokes_shared_helper(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        loop = MagicMock()
        loop.run_oneshot.return_value = MagicMock(
            text='{"fixes": [], "commit_message": "m", "summary": "s"}',
        )
        developer = reviewer_module.Developer(loop=loop)

        spy = MagicMock(wraps=reviewer_module.retry_with_backoff)
        monkeypatch.setattr(reviewer_module, "retry_with_backoff", spy)

        developer._call_with_retry("prompt", spec_id="SP-X")

        spy.assert_called_once()
        assert spy.call_args.kwargs["log_scope"] == "review.developer"
