"""One automatic second attempt of a review that failed on transport errors only.

SP-REVIEW-SURVIVES-TRANSPORT-FAILURES G2. A network drop longer than the
transport window of one model call
(:mod:`repoach.agent_engine.transport_window`) still leaves a review
incomplete: ``review pr`` exits 3 (SP-REVIEW-TRANSPORT-ERROR-VERDICT),
which stays the right verdict. This module decides whether that run may
be tried once more, by itself, at the same head:

* :func:`incomplete_cause` names why an ``ERROR`` review is incomplete:
  ``transport`` when every reviewer that errored did so on a transport
  error and every finding the refuter deferred was deferred because its
  judge call failed on one; ``other`` for anything else. ``review pr``
  prints it in its JSON as ``incomplete_cause``.
* :func:`dispatch_review_retry` records the retry in the PR's merge
  attempts comment, then dispatches ``auto-review.yml`` with
  ``retry_of`` naming the failed run. It refuses when the PR is not open,
  when its head moved, or when that comment already holds a retry at the
  head: never more than one automatic retry per head.
* :func:`record_retry_ran` lets the dispatched run name itself in the
  same comment, so the comment names both runs.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from ..core.logging import get_logger
from ..forge import Forge, ForgeError
from .merge_wait import (
    MERGE_ATTEMPTS_MARKER,
    MERGE_RULE_VERSION,
    MergeAttempt,
    parse_attempts_comment,
    upsert_attempt,
)
from .refuter import JUDGE_CALL_FAILED, Refutation
from .reviewer import ReviewerOutcome, ReviewVerdict

_log = get_logger(__name__)

IncompleteCause = Literal["transport", "other"]

TRANSPORT_ERROR_NAMES: frozenset[str] = frozenset(
    {"GatewayTransportError", "GatewayChainExhausted"}
)
"""The error types a review may be retried for: the model was unreachable, not wrong.

``GatewayChainExhausted`` is the proxy saying every upstream failed: the
same outage seen one hop further."""

REVIEW_RETRY_OUTCOME = "REVIEW_RETRY"
"""The merge-attempt outcome of an automatic review retry (never a ``SKIP_``)."""

AUTO_REVIEW_WORKFLOW = "auto-review.yml"
"""The workflow a retry dispatches."""

REVIEW_RETRIES_PER_HEAD = 1
"""Automatic retries one head may get (``review_retries_per_head``)."""

REVIEW_RETRY_DELAY_S = 300
"""Seconds the retry job waits before it dispatches (``review_retry_delay_s``)."""

_TRANSPORT_SUMMARY_RE = re.compile(
    r"^\[parse_failed:TRANSPORT\] all \d+ attempts raised — last=(?P<name>\w+)"
)
_CRASH_SUMMARY_RE = re.compile(r"^_\(bot crashed: (?P<name>\w+) —")


def _errored_on_transport(outcome: ReviewerOutcome) -> bool:
    """Return True when an ``ERROR`` reviewer's summary names a transport error as its cause."""
    for pattern in (_TRANSPORT_SUMMARY_RE, _CRASH_SUMMARY_RE):
        match = pattern.match(outcome.summary)
        if match is not None:
            return match.group("name") in TRANSPORT_ERROR_NAMES
    return False


def _deferred_on_transport(refutation: Refutation) -> bool:
    """Return True when a deferral's reason is a judge call that failed on a transport error."""
    prefix = f"{JUDGE_CALL_FAILED}: "
    if not refutation.reason.startswith(prefix):
        return False
    return refutation.reason.removeprefix(prefix) in TRANSPORT_ERROR_NAMES


def incomplete_cause(
    final_verdict: ReviewVerdict,
    reviews: Sequence[ReviewerOutcome],
    refutations: Sequence[Refutation],
) -> IncompleteCause | None:
    """Name why a review is incomplete.

    Args:
        final_verdict: The team verdict; only ``ERROR`` is incomplete.
        reviews: The reviewers' outcomes.
        refutations: What the refuter did with each finding of the run.

    Returns:
        None for a complete review; ``transport`` when at least one
        reviewer errored, every one that did errored on a transport error,
        and every deferred finding was deferred on a transport error of
        its judge; ``other`` otherwise (a reviewer that could not parse,
        a refused credential, a deferral for any other reason, an
        incomplete review with no errored reviewer).
    """
    if final_verdict != ReviewVerdict.ERROR:
        return None
    errored = [o for o in reviews if o.verdict == ReviewVerdict.ERROR]
    if not errored or not all(_errored_on_transport(o) for o in errored):
        return "other"
    deferred = [r for r in refutations if r.state == "deferred"]
    if not all(_deferred_on_transport(r) for r in deferred):
        return "other"
    return "transport"


class RetryDecision(BaseModel):
    """What :func:`dispatch_review_retry` did.

    Attributes:
        dispatched: True when the retry run was dispatched.
        line: One line naming what was done or why not.
    """

    dispatched: bool
    line: str


def _attempts_at_head(forge: Forge, pr_number: int, head_sha: str) -> list[MergeAttempt]:
    """The retries already recorded at ``head_sha``.

    Raises:
        ForgeError: The comment could not be listed.
        ValueError: The comment exists but cannot be read.
    """
    comment = forge.find_marked_comment(pr_number, MERGE_ATTEMPTS_MARKER)
    attempts = parse_attempts_comment(comment.body) if comment is not None else []
    return [a for a in attempts if a.outcome == REVIEW_RETRY_OUTCOME and a.head_sha == head_sha]


def dispatch_review_retry(
    forge: Forge, pr_number: int, *, head_sha: str, failed_run: str, now: datetime
) -> RetryDecision:
    """Record one retry of ``failed_run`` at ``head_sha``, then dispatch it.

    The record is written before the dispatch, so a second failed run at
    the same head finds it and refuses; a record that cannot be written,
    or a comment that cannot be read, refuses the retry (fail closed: no
    record, no retry).

    Args:
        forge: The forge.
        pr_number: The PR.
        head_sha: The head the failed run reviewed.
        failed_run: The id of the run that exited 3.
        now: The time recorded on the attempt.

    Returns:
        The decision and its line.

    Raises:
        ForgeError: The PR could not be read or the dispatch was refused.
    """
    pr = forge.pull_request(pr_number)
    sha7 = head_sha[:7]
    if pr.state != "open" or pr.draft:
        state = "a draft" if pr.draft else pr.state
        return RetryDecision(
            dispatched=False, line=f"review retry: #{pr_number} is {state}, not retried"
        )
    if pr.head_sha != head_sha:
        return RetryDecision(
            dispatched=False,
            line=(
                f"review retry: the head moved from {sha7} to {pr.head_sha[:7]}, not retried "
                "(the push starts its own review)"
            ),
        )
    try:
        prior = _attempts_at_head(forge, pr_number, head_sha)
    except (ForgeError, ValueError) as exc:
        return RetryDecision(
            dispatched=False,
            line=f"review retry: the merge attempts comment is unreadable ({str(exc)[:160]}), not retried",
        )
    if len(prior) >= REVIEW_RETRIES_PER_HEAD:
        return RetryDecision(
            dispatched=False,
            line=f"review retry: {sha7} was already retried ({prior[0].trigger}), not retried again",
        )
    attempt = MergeAttempt(
        pr_number=pr_number,
        head_sha=head_sha,
        outcome=REVIEW_RETRY_OUTCOME,
        reason=(
            f"review run {failed_run} incomplete on transport errors only; "
            f"one automatic retry dispatched at {sha7}"
        ),
        trigger=f"auto-review:{failed_run}",
        rule_version=MERGE_RULE_VERSION,
        recorded_at=now,
    )
    if not upsert_attempt(forge, attempt):
        return RetryDecision(
            dispatched=False,
            line="review retry: the retry could not be recorded in the merge attempts comment, not retried",
        )
    forge.dispatch_workflow(
        AUTO_REVIEW_WORKFLOW, {"pr_number": str(pr_number), "retry_of": failed_run}
    )
    _log.info(
        "review_retry.dispatched", pr_number=pr_number, head_sha=head_sha, failed_run=failed_run
    )
    return RetryDecision(
        dispatched=True,
        line=f"review retry: run {failed_run} incomplete on transport errors, retry dispatched at {sha7}",
    )


def record_retry_ran(
    forge: Forge,
    pr_number: int,
    *,
    head_sha: str,
    run_id: str,
    retry_of: str,
    result: str,
    now: datetime,
) -> str:
    """Name the retry run and its result in the merge attempts comment.

    Args:
        forge: The forge.
        pr_number: The PR.
        head_sha: The head the retry reviewed.
        run_id: The retry run's id.
        retry_of: The id of the run it retried.
        result: The retry's review job result (``success``, ``failure``…).
        now: The time recorded on the attempt.

    Returns:
        One line naming what was recorded.
    """
    sha7 = head_sha[:7]
    attempt = MergeAttempt(
        pr_number=pr_number,
        head_sha=head_sha,
        outcome=REVIEW_RETRY_OUTCOME,
        reason=(
            f"review run {run_id} retried review run {retry_of}: {result}; "
            f"no further automatic retry at {sha7}"
        ),
        trigger=f"dispatch:{run_id}",
        rule_version=MERGE_RULE_VERSION,
        recorded_at=now,
    )
    state = "recorded" if upsert_attempt(forge, attempt) else "not recorded"
    return f"review retry: run {run_id} retried run {retry_of} at {sha7}: {result} ({state})"


__all__ = [
    "AUTO_REVIEW_WORKFLOW",
    "REVIEW_RETRIES_PER_HEAD",
    "REVIEW_RETRY_DELAY_S",
    "REVIEW_RETRY_OUTCOME",
    "TRANSPORT_ERROR_NAMES",
    "IncompleteCause",
    "RetryDecision",
    "dispatch_review_retry",
    "incomplete_cause",
    "record_retry_ran",
]
