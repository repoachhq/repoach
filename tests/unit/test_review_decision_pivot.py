"""Unit tests for ``pr_reviews.decision_pivot`` (SP-WA-DECISION-TRACE 2c).

The pivot is the one-line summary the EXPLAIN path renders when the
user asks "pourquoi PR #N a-t-elle été refusée ?".  It points at the
specific blocker (or major) that drove a REQUEST_CHANGES verdict ;
APPROVE / COMMENT verdicts leave it ``None`` since no single
comment forced the call.
"""

from __future__ import annotations

from ferova.review.persistence import derive_decision_pivot
from ferova.review.reviewer import (
    BotRole,
    ReviewComment,
    ReviewerOutcome,
    ReviewVerdict,
)


def _outcome(
    *,
    verdict: ReviewVerdict,
    comments: list[ReviewComment],
    summary: str = "",
) -> ReviewerOutcome:
    return ReviewerOutcome(
        role=BotRole.SENTINEL,
        verdict=verdict,
        summary=summary,
        comments=comments,
        model_used="kimi-k2-instruct",
        elapsed_s=0.5,
        tokens_used=1234,
    )


def test_pivot_is_none_for_approve_verdict() -> None:
    outcome = _outcome(verdict=ReviewVerdict.APPROVE, comments=[])
    assert derive_decision_pivot(outcome) is None


def test_pivot_is_none_for_comment_verdict_with_minors() -> None:
    outcome = _outcome(
        verdict=ReviewVerdict.COMMENT,
        comments=[ReviewComment(file="x.py", line=1, severity="minor", body="style nit")],
    )
    assert derive_decision_pivot(outcome) is None


def test_pivot_points_at_first_blocker_when_request_changes() -> None:
    outcome = _outcome(
        verdict=ReviewVerdict.REQUEST_CHANGES,
        comments=[
            ReviewComment(file="a.py", line=10, severity="major", body="big concern"),
            ReviewComment(file="b.py", line=42, severity="blocker", body="security hole"),
        ],
    )
    pivot = derive_decision_pivot(outcome)
    assert pivot is not None
    assert "blocker" in pivot
    assert "b.py:42" in pivot
    assert "security hole" in pivot


def test_pivot_falls_back_to_first_major_when_no_blocker() -> None:
    outcome = _outcome(
        verdict=ReviewVerdict.REQUEST_CHANGES,
        comments=[
            ReviewComment(file="a.py", line=10, severity="major", body="big concern"),
            ReviewComment(file="b.py", line=42, severity="minor", body="nit"),
        ],
    )
    pivot = derive_decision_pivot(outcome)
    assert pivot is not None
    assert "major @ a.py:10" in pivot
    assert "big concern" in pivot


def test_pivot_handles_verdict_level_request_changes_without_comments() -> None:
    outcome = _outcome(
        verdict=ReviewVerdict.REQUEST_CHANGES,
        comments=[],
        summary="Tests are red on Python 3.13.",
    )
    pivot = derive_decision_pivot(outcome)
    assert pivot is not None
    assert pivot.startswith("verdict-only:")
    assert "Tests are red" in pivot


def test_pivot_truncates_long_comment_bodies() -> None:
    long_body = "x" * 500
    outcome = _outcome(
        verdict=ReviewVerdict.REQUEST_CHANGES,
        comments=[ReviewComment(file="a.py", line=1, severity="blocker", body=long_body)],
    )
    pivot = derive_decision_pivot(outcome)
    assert pivot is not None
    assert "…" in pivot
    assert len(pivot) < 250
