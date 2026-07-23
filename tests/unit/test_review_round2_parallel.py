"""Round-2 confirm-or-retract re-reviews run pooled (SP-REVIEW-ROUND2-PARALLEL).

Proves ``ReviewTeamOrchestrator._round_two`` submits every triggered
reviewer's ``review_diff`` call to a ``ThreadPoolExecutor`` capped by
``self._max_workers`` instead of invoking them one at a time in a
sequential ``for`` loop:

- AC1: a shared ``threading.Barrier(parties=2)`` releases only when two
  triggered fake reviewers' ``review_diff`` calls run concurrently — this
  deadlocks (``BrokenBarrierError``) under the pre-change sequential
  loop and only passes once round 2 is pooled.
- AC2: an exception from one pooled fake keeps that reviewer's round-1
  outcome, logs ``review_team.round_two_failed``, and never disturbs the
  other triggered reviewer's outcome.
- AC3: fakes that complete in inverted order (via event staggering, no
  sleeps) still land at their own reviewer index.
- AC5: the executor is constructed with ``max_workers=self._max_workers``.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest
import structlog
from structlog.testing import capture_logs

import repoach.review.orchestrator as orchestrator_module
from repoach.review.orchestrator import ReviewTeamOrchestrator
from repoach.review.reviewer import (
    Architect,
    BotRole,
    ReviewComment,
    Reviewer,
    ReviewerOutcome,
    ReviewVerdict,
    Scribe,
    Sentinel,
    Tester,
)

_ROLE_TO_CLASS: dict[BotRole, type[Reviewer]] = {
    BotRole.ARCHITECT: Architect,
    BotRole.SENTINEL: Sentinel,
    BotRole.TESTER: Tester,
    BotRole.SCRIBE: Scribe,
}


def _outcome(
    *,
    role: BotRole,
    verdict: ReviewVerdict,
    comments: list[ReviewComment] | None = None,
    summary: str = "summary",
) -> ReviewerOutcome:
    return ReviewerOutcome(
        role=role,
        verdict=verdict,
        summary=summary,
        comments=comments or [],
        model_used="test-model",
        elapsed_s=0.1,
        tokens_used=10,
        raw_response="{}",
    )


def _reviewer(role: BotRole) -> Reviewer:
    return _ROLE_TO_CLASS[role](loop=MagicMock())


def _orchestrator(tmp_path, max_workers: int = 4) -> ReviewTeamOrchestrator:
    gh = MagicMock()
    return ReviewTeamOrchestrator(
        gh=gh,
        db_path=tmp_path / "l4.db",
        post_to_github=False,
        max_workers=max_workers,
    )


@pytest.fixture(autouse=True)
def _fresh_orchestrator_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rebind the module logger so ``capture_logs`` sees its events.

    ``configure_logging`` (exercised by earlier suites in serial order)
    sets ``cache_logger_on_first_use=True``; a proxy cached before this
    test keeps its materialized processor chain and bypasses the
    ``capture_logs`` swap. A fresh lazy proxy binds inside the capture
    context instead.
    """
    monkeypatch.setattr(
        orchestrator_module, "_log", structlog.get_logger("review.orchestrator.test")
    )


def _blocker_outcome(role: BotRole) -> ReviewerOutcome:
    return _outcome(
        role=role,
        verdict=ReviewVerdict.REQUEST_CHANGES,
        comments=[ReviewComment(file="x.py", line=1, severity="blocker", body="b")],
    )


def test_round_two_barrier_releases_only_when_pooled(tmp_path):
    """AC1: two triggered fakes rendezvous on a Barrier(parties=2, timeout=10).

    Under the pre-change sequential loop this call never releases because
    the second reviewer's review_diff is invoked only after the first one
    already returned, so the barrier times out with BrokenBarrierError.
    Pooled round 2 submits both before waiting on either result, so both
    threads reach the barrier together.
    """
    orch = _orchestrator(tmp_path)
    reviewers = [_reviewer(BotRole.ARCHITECT), _reviewer(BotRole.SENTINEL)]
    round1 = [_blocker_outcome(BotRole.ARCHITECT), _blocker_outcome(BotRole.SENTINEL)]
    barrier = threading.Barrier(parties=2, timeout=10)

    def _make_review_diff(role: BotRole):
        def _review_diff(diff, **kwargs):
            barrier.wait()
            return _outcome(role=role, verdict=ReviewVerdict.APPROVE)

        return _review_diff

    reviewers[0].review_diff = _make_review_diff(BotRole.ARCHITECT)
    reviewers[1].review_diff = _make_review_diff(BotRole.SENTINEL)

    revised = orch._round_two(
        reviewers=reviewers,
        round1_outcomes=round1,
        spec_plan=None,
        guard_events=[],
        diff="diff",
        pr_number=1,
    )

    assert revised[0].verdict is ReviewVerdict.APPROVE
    assert revised[1].verdict is ReviewVerdict.APPROVE


def test_round_two_failure_isolation_keeps_round1_for_failing_reviewer(tmp_path):
    """AC2: one pooled fake raises; the other's outcome still lands."""
    orch = _orchestrator(tmp_path)
    reviewers = [_reviewer(BotRole.ARCHITECT), _reviewer(BotRole.SENTINEL)]
    round1 = [_blocker_outcome(BotRole.ARCHITECT), _blocker_outcome(BotRole.SENTINEL)]

    def _architect_review_diff(diff, **kwargs):
        raise RuntimeError("boom")

    def _sentinel_review_diff(diff, **kwargs):
        return _outcome(role=BotRole.SENTINEL, verdict=ReviewVerdict.APPROVE)

    reviewers[0].review_diff = _architect_review_diff
    reviewers[1].review_diff = _sentinel_review_diff

    with capture_logs() as events:
        revised = orch._round_two(
            reviewers=reviewers,
            round1_outcomes=round1,
            spec_plan=None,
            guard_events=[],
            diff="diff",
            pr_number=1,
        )

    assert revised[0] is round1[0]
    assert revised[1].verdict is ReviewVerdict.APPROVE

    failed_events = [e for e in events if e.get("event") == "review_team.round_two_failed"]
    assert len(failed_events) == 1
    assert failed_events[0]["role"] == BotRole.ARCHITECT.value

    done_events = [e for e in events if e.get("event") == "review_team.round_two_done"]
    assert len(done_events) == 1
    assert done_events[0]["role"] == BotRole.SENTINEL.value


def test_round_two_index_mapping_survives_inverted_completion_order(tmp_path):
    """AC3: fakes finishing in inverted order still land by reviewer index."""
    orch = _orchestrator(tmp_path)
    reviewers = [
        _reviewer(BotRole.ARCHITECT),
        _reviewer(BotRole.SENTINEL),
        _reviewer(BotRole.TESTER),
    ]
    round1 = [
        _blocker_outcome(BotRole.ARCHITECT),
        _blocker_outcome(BotRole.SENTINEL),
        _blocker_outcome(BotRole.TESTER),
    ]

    architect_may_finish = threading.Event()
    sentinel_finished = threading.Event()
    tester_finished = threading.Event()

    def _architect_review_diff(diff, **kwargs):
        architect_may_finish.wait(timeout=10)
        return _outcome(role=BotRole.ARCHITECT, verdict=ReviewVerdict.APPROVE, summary="architect")

    def _sentinel_review_diff(diff, **kwargs):
        result = _outcome(role=BotRole.SENTINEL, verdict=ReviewVerdict.COMMENT, summary="sentinel")
        sentinel_finished.set()
        return result

    def _tester_review_diff(diff, **kwargs):
        sentinel_finished.wait(timeout=10)
        result = _outcome(
            role=BotRole.TESTER, verdict=ReviewVerdict.REQUEST_CHANGES, summary="tester"
        )
        tester_finished.set()
        architect_may_finish.set()
        return result

    reviewers[0].review_diff = _architect_review_diff
    reviewers[1].review_diff = _sentinel_review_diff
    reviewers[2].review_diff = _tester_review_diff

    revised = orch._round_two(
        reviewers=reviewers,
        round1_outcomes=round1,
        spec_plan=None,
        guard_events=[],
        diff="diff",
        pr_number=1,
    )

    assert revised[0].role is BotRole.ARCHITECT
    assert revised[0].verdict is ReviewVerdict.APPROVE
    assert revised[0].summary == "architect"
    assert revised[1].role is BotRole.SENTINEL
    assert revised[1].verdict is ReviewVerdict.COMMENT
    assert revised[1].summary == "sentinel"
    assert revised[2].role is BotRole.TESTER
    assert revised[2].verdict is ReviewVerdict.REQUEST_CHANGES
    assert revised[2].summary == "tester"


def test_round_two_pool_capped_at_max_workers(tmp_path, monkeypatch):
    """AC5: the executor is constructed with max_workers=self._max_workers."""
    orch = _orchestrator(tmp_path, max_workers=2)
    reviewers = [_reviewer(BotRole.ARCHITECT), _reviewer(BotRole.SENTINEL)]
    round1 = [_blocker_outcome(BotRole.ARCHITECT), _blocker_outcome(BotRole.SENTINEL)]

    reviewers[0].review_diff = lambda diff, **kwargs: _outcome(
        role=BotRole.ARCHITECT, verdict=ReviewVerdict.APPROVE
    )
    reviewers[1].review_diff = lambda diff, **kwargs: _outcome(
        role=BotRole.SENTINEL, verdict=ReviewVerdict.APPROVE
    )

    captured_max_workers: list[int | None] = []
    real_executor_init = ThreadPoolExecutor.__init__

    def _capturing_init(self, *args, max_workers=None, **kwargs):
        captured_max_workers.append(max_workers)
        return real_executor_init(self, *args, max_workers=max_workers, **kwargs)

    monkeypatch.setattr(ThreadPoolExecutor, "__init__", _capturing_init)

    orch._round_two(
        reviewers=reviewers,
        round1_outcomes=round1,
        spec_plan=None,
        guard_events=[],
        diff="diff",
        pr_number=1,
    )

    assert 2 in captured_max_workers
