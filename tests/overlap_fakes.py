"""Structural overlap evidence for the fresh-head guard, shared by both suites.

A wall-clock bound on ``review_pr`` measures the machine as much as the code:
on a runner at load average 35-42 the overlap held while the run took 2.63s
against a 2.10s bound. These helpers prove the guard overlapped the reviewer
fan-out by construction instead. The guard's injected ``sleep`` blocks on its
first call until a reviewer has entered ``review_diff``, and every instrumented
call records its ``(start, end)`` interval and the number of instrumented calls
in flight when it entered. A guard awaited before the fan-out cannot see a
reviewer start, so it times out at the rendezvous and the recorded maximum
concurrency stays at one; a guard running alongside the fan-out is still inside
its call when the first reviewer enters, whatever the load.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from repoach.review.reviewer import ReviewerOutcome

RENDEZVOUS_TIMEOUT_S = 20.0
SANITY_BOUND_S = 30.0


@dataclass
class ConcurrencyTracker:
    """Shared record of which instrumented calls run, when, and how many at once.

    Every instrumented call (the guard and each reviewer) brackets itself with
    :meth:`enter` and :meth:`leave`; ``max_running`` is the largest number of
    such calls observed in flight at the same instant, ``intervals`` keeps each
    call's ``(start, end)`` monotonic timestamps by name, and ``rendezvous``
    receives one boolean per guard rendezvous: whether a reviewer entered in time.
    """

    lock: threading.Lock = field(default_factory=threading.Lock)
    running: int = 0
    max_running: int = 0
    intervals: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    reviewer_entered: threading.Event = field(default_factory=threading.Event)
    rendezvous: list[bool] = field(default_factory=list)

    def enter(self) -> float:
        """Count one more call in flight and return its start timestamp."""
        with self.lock:
            self.running += 1
            self.max_running = max(self.max_running, self.running)
            return time.monotonic()

    def leave(self, name: str, start: float) -> None:
        """Count one call fewer in flight and record its interval under *name*."""
        with self.lock:
            self.running -= 1
            self.intervals.setdefault(name, []).append((start, time.monotonic()))


def make_tracked_review_diff(
    outcome: ReviewerOutcome, tracker: ConcurrencyTracker
) -> Callable[..., ReviewerOutcome]:
    """Return a review_diff replacement that records its interval and signals entry."""

    def _review_diff(diff: str, **kwargs: object) -> ReviewerOutcome:
        start = tracker.enter()
        tracker.reviewer_entered.set()
        tracker.leave("reviewer", start)
        return outcome

    return _review_diff


def make_tracked_guard(
    real_guard: Callable[..., str | None], tracker: ConcurrencyTracker
) -> Callable[..., str | None]:
    """Wrap the real ``resolve_fresh_head`` so its call is tracked and rendezvous-bound.

    The wrapper calls the unmodified guard with ``delay_s=0.0`` and an injected
    ``sleep`` whose first call blocks until a reviewer has entered
    ``review_diff`` (bounded by :data:`RENDEZVOUS_TIMEOUT_S`); later calls
    return at once.
    """

    def _sleep(_delay_s: float) -> None:
        if not tracker.rendezvous:
            tracker.rendezvous.append(tracker.reviewer_entered.wait(RENDEZVOUS_TIMEOUT_S))

    def _guard(forge: object, pr_number: int, *, repo_root: Path) -> str | None:
        start = tracker.enter()
        try:
            return real_guard(forge, pr_number, repo_root=repo_root, delay_s=0.0, sleep=_sleep)
        finally:
            tracker.leave("guard", start)

    return _guard


def _intersects(a: tuple[float, float], b: tuple[float, float]) -> bool:
    """Whether two closed ``(start, end)`` intervals share at least one instant."""
    return a[0] <= b[1] and b[0] <= a[1]


def assert_guard_overlapped_fanout(
    tracker: ConcurrencyTracker, *, n_reviewers: int, wall_s: float
) -> None:
    """Assert the guard was inside its call while a reviewer ran.

    Checks the rendezvous succeeded, the guard ran once and every reviewer ran,
    at least two instrumented calls were in flight at once, the guard's interval
    intersects a reviewer interval, and *wall_s* stays under the generous
    :data:`SANITY_BOUND_S` ceiling.
    """
    assert tracker.rendezvous == [True], (
        f"Guard rendezvous {tracker.rendezvous!r}: no reviewer entered review_diff "
        "while the guard was blocked in its first propagation-lag sleep; the guard "
        "did NOT overlap the reviewer fan-out"
    )
    guard_intervals = tracker.intervals.get("guard", [])
    reviewer_intervals = tracker.intervals.get("reviewer", [])
    assert len(guard_intervals) == 1, f"Guard ran {len(guard_intervals)} times, expected 1"
    assert len(reviewer_intervals) == n_reviewers, (
        f"Reviewers ran {len(reviewer_intervals)} times, expected {n_reviewers}"
    )
    assert tracker.max_running >= 2, (
        f"Max concurrent instrumented calls {tracker.max_running}; the guard did NOT "
        "overlap the reviewer fan-out"
    )
    assert any(_intersects(guard_intervals[0], r) for r in reviewer_intervals), (
        f"Guard interval {guard_intervals[0]} intersects no reviewer interval {reviewer_intervals}"
    )
    assert wall_s < SANITY_BOUND_S, f"review_pr took {wall_s:.2f}s, sanity bound {SANITY_BOUND_S}s"
