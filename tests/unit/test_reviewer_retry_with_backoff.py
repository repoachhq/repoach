"""Unit tests for SP-REVIEWER-RETRY-WITH-BACKOFF.

Covers :meth:`Reviewer._call_with_retry` — the retry-with-backoff
wrapper around ``AgentLoop.run_oneshot`` + ``_parse_response`` ported
from the :class:`Developer` runner.  Two retry triggers are exercised :

1. ``run_oneshot`` raises any exception (transport exhaustion).
2. ``_parse_response`` returns a ``[parse_failed:…]`` summary
   (MALFORMED / TRUNCATED / TRANSPORT).

Tests use the :class:`Architect` reviewer as the representative
subclass — the retry logic lives on the base :class:`Reviewer` so the
same behaviour applies to Sentinel / Tester / Scribe.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from repoach.review.reviewer import (
    Architect,
    ReviewComment,
    ReviewVerdict,
    _FailedRunResult,
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip real backoff waits so the 30 s + 90 s schedule doesn't
    bleed into test runtime.  The retry semantics are exercised
    regardless ; the sleep itself is an implementation detail.
    """
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda *_a, **_kw: None)


def _result(text: str, *, model: str = "nvidia_nim/test", elapsed: float = 0.1) -> Any:
    """Build a minimal NimAgentOutput-shaped result for run_oneshot mocks."""
    result = MagicMock()
    result.text = text
    result.model_used = model
    result.elapsed_s = elapsed
    result.tokens_used = 100
    return result


def _clean_json() -> str:
    return '{"verdict": "APPROVE", "summary": "looks good", "comments": []}'


class TestRetryOnSuccess:
    """First attempt succeeds with clean JSON → no retry."""

    def test_first_attempt_succeeds_returns_immediately(self) -> None:
        loop = MagicMock()
        loop.run_oneshot.return_value = _result(_clean_json())
        reviewer = Architect(loop=loop)

        verdict, summary, comments, result = reviewer._call_with_retry(
            "ignored prompt",
            pr_number=42,
        )

        assert loop.run_oneshot.call_count == 1
        assert verdict == ReviewVerdict.APPROVE
        assert summary == "looks good"
        assert comments == []
        assert result.model_used == "nvidia_nim/test"


class TestRetryOnTransportException:
    """When run_oneshot raises, the next attempt is invoked."""

    def test_first_attempt_raises_second_succeeds(self) -> None:
        loop = MagicMock()
        loop.run_oneshot.side_effect = [
            RuntimeError("simulated transport flake"),
            _result(_clean_json()),
        ]
        reviewer = Architect(loop=loop)

        verdict, summary, _, _ = reviewer._call_with_retry(
            "ignored",
            pr_number=None,
        )

        assert loop.run_oneshot.call_count == 2
        assert verdict == ReviewVerdict.APPROVE
        assert summary == "looks good"

    def test_all_attempts_raise_returns_transport_stub(self) -> None:
        loop = MagicMock()
        loop.run_oneshot.side_effect = [
            RuntimeError("flake-1"),
            RuntimeError("flake-2"),
            RuntimeError("flake-3"),
        ]
        reviewer = Architect(loop=loop)

        verdict, summary, comments, result = reviewer._call_with_retry(
            "ignored",
            pr_number=None,
        )

        assert loop.run_oneshot.call_count == 3
        assert verdict == ReviewVerdict.COMMENT
        assert "[parse_failed:TRANSPORT]" in summary
        assert "RuntimeError" in summary
        assert comments == []
        assert isinstance(result, _FailedRunResult)
        assert result.model_used == "exhausted"


class TestRetryOnParseFailed:
    """When _parse_response returns ``[parse_failed:…]`` the call retries."""

    def test_parse_failed_first_clean_second(self) -> None:
        loop = MagicMock()
        loop.run_oneshot.side_effect = [
            _result(" "),
            _result(_clean_json()),
        ]
        reviewer = Architect(loop=loop)

        verdict, summary, _, _ = reviewer._call_with_retry(
            "ignored",
            pr_number=None,
        )

        assert loop.run_oneshot.call_count == 2
        assert verdict == ReviewVerdict.APPROVE
        assert summary == "looks good"

    def test_parse_failed_truncated_first_clean_second(self) -> None:
        loop = MagicMock()
        loop.run_oneshot.side_effect = [
            _result('{"verdict": "APPROVE", "summa'),
            _result(_clean_json()),
        ]
        reviewer = Architect(loop=loop)

        verdict, summary, _, _ = reviewer._call_with_retry(
            "ignored",
            pr_number=None,
        )

        assert loop.run_oneshot.call_count == 2
        assert verdict == ReviewVerdict.APPROVE
        assert summary == "looks good"

    def test_all_attempts_parse_failed_returns_last_marker(self) -> None:
        loop = MagicMock()
        loop.run_oneshot.side_effect = [
            _result(""),
            _result("   "),
            _result("\n\t "),
        ]
        reviewer = Architect(loop=loop)

        verdict, summary, comments, result = reviewer._call_with_retry(
            "ignored",
            pr_number=None,
        )

        assert loop.run_oneshot.call_count == 3
        assert verdict == ReviewVerdict.COMMENT
        assert summary.startswith("[parse_failed:TRANSPORT]")
        assert comments == []
        assert not isinstance(result, _FailedRunResult)
        assert result.text == "\n\t "


class TestRetryMixedFailureModes:
    """Realistic mix : exception → parse_failed → success."""

    def test_exception_then_parse_failed_then_success(self) -> None:
        loop = MagicMock()
        loop.run_oneshot.side_effect = [
            RuntimeError("kimi down"),
            _result(" "),
            _result(_clean_json()),
        ]
        reviewer = Architect(loop=loop)

        verdict, summary, _, _ = reviewer._call_with_retry(
            "ignored",
            pr_number=None,
        )

        assert loop.run_oneshot.call_count == 3
        assert verdict == ReviewVerdict.APPROVE
        assert summary == "looks good"


class TestReviewDiffIntegratesRetry:
    """End-to-end via the public review_diff() entry point."""

    def test_review_diff_returns_outcome_after_retry(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """review_diff dispatches through _call_with_retry and surfaces a clean outcome."""
        loop = MagicMock()
        loop.run_oneshot.side_effect = [
            _result(" "),
            _result(
                '{"verdict": "REQUEST_CHANGES", "summary": "needs work",'
                ' "comments": [{"file": "a.py", "line": 1,'
                ' "severity": "blocker", "body": "fix"}]}'
            ),
        ]
        persona = tmp_path / Architect.persona_filename
        persona.write_text("Review this diff:\n{DIFF}\n{SPEC_PLAN}\n")
        monkeypatch.setattr("repoach.review.reviewer._PROMPTS_DIR", tmp_path)

        reviewer = Architect(loop=loop)
        outcome = reviewer.review_diff("--- a.py\n+++ b.py\n", pr_number=123)

        assert loop.run_oneshot.call_count == 2
        assert outcome.verdict == ReviewVerdict.REQUEST_CHANGES
        assert outcome.summary == "needs work"
        assert outcome.comments == [
            ReviewComment(file="a.py", line=1, severity="blocker", body="fix"),
        ]
