"""Replay a completed ``review pr`` run at the same head (SP-FINDINGS-BRIDGE-DOCFIX 0.3).

The unit of a review is the run: once a run at ``(pr, head)`` completed
(every reviewer parsed, the findings written), a later ``review pr`` at
that head does not call the reviewers again. It reads back what the run
stored here: the reviewer outcomes it would post, the refutations it
reported, and the spec it ran against. ``--force`` re-reviews.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime

from .findings import ClaimType, ReviewRun
from .refuter import Refutation
from .reviewer import BotRole, ReviewComment, ReviewerOutcome, ReviewVerdict

FORCE_HINT = "use --force to re-review"


def outcome_to_json(outcome: ReviewerOutcome) -> dict[str, object]:
    """Return ``outcome`` as a JSON-serialisable dict (:func:`outcome_from_json` inverts it)."""
    return {
        "role": outcome.role.value,
        "verdict": outcome.verdict.value,
        "summary": outcome.summary,
        "comments": [asdict(comment) for comment in outcome.comments],
        "model_used": outcome.model_used,
        "elapsed_s": outcome.elapsed_s,
        "tokens_used": outcome.tokens_used,
        "trace": list(outcome.trace),
    }


def outcome_from_json(data: dict[str, object]) -> ReviewerOutcome:
    """Rebuild a :class:`ReviewerOutcome` stored by :func:`outcome_to_json`."""
    comments = data.get("comments") or []
    trace = data.get("trace") or []
    return ReviewerOutcome(
        role=BotRole(str(data["role"])),
        verdict=ReviewVerdict(str(data["verdict"])),
        summary=str(data.get("summary", "")),
        comments=[ReviewComment(**comment) for comment in comments],
        model_used=str(data.get("model_used", "")),
        elapsed_s=float(data.get("elapsed_s", 0.0)),
        tokens_used=int(data.get("tokens_used", 0)),
        trace=list(trace),
    )


def refutation_to_json(refutation: Refutation) -> dict[str, object]:
    """Return ``refutation`` as a JSON-serialisable dict."""
    return {
        "finding_id": refutation.finding_id,
        "file": refutation.file,
        "line_start": refutation.line_start,
        "claim_type": refutation.claim_type.value,
        "state": refutation.state,
        "reason": refutation.reason,
    }


def refutation_from_json(data: dict[str, object]) -> Refutation:
    """Rebuild a :class:`Refutation` stored by :func:`refutation_to_json`."""
    finding_id = data.get("finding_id")
    return Refutation(
        finding_id=None if finding_id is None else int(finding_id),
        file=str(data["file"]),
        line_start=int(data["line_start"]),
        claim_type=ClaimType(str(data["claim_type"])),
        state=data["state"],
        reason=str(data.get("reason", "")),
    )


def completed_run(
    *,
    run_id: str,
    pr_number: int,
    head_sha: str,
    final_verdict: ReviewVerdict,
    outcomes: list[ReviewerOutcome],
    refutations: list[Refutation],
    spec: dict[str, object],
) -> ReviewRun:
    """Return the :class:`ReviewRun` a completed review stores for replay.

    Args:
        run_id: The usage run id of the review (`repoach usage`), also stamped
            on the findings it recorded.
        pr_number: The PR / MR reviewed.
        head_sha: The head reviewed.
        final_verdict: The team verdict reported.
        outcomes: The reviewer outcomes after the hallucination guard and
            round 2 (what would be posted).
        refutations: What the refuter did with each finding it saw.
        spec: ``spec_id``, ``spec_source``, ``spec_covered``, ``spec_missing``.

    Returns:
        The run, timestamped now (UTC).
    """
    return ReviewRun(
        run_id=run_id,
        pr_number=pr_number,
        head_sha=head_sha,
        completed_at=datetime.now(UTC).isoformat(timespec="seconds"),
        final_verdict=final_verdict.value,
        payload={
            "outcomes": [outcome_to_json(outcome) for outcome in outcomes],
            "refutations": [refutation_to_json(refutation) for refutation in refutations],
            "spec": dict(spec),
        },
    )


def replayed_outcomes(run: ReviewRun) -> list[ReviewerOutcome]:
    """Return the reviewer outcomes ``run`` stored."""
    return [outcome_from_json(item) for item in run.payload.get("outcomes", [])]


def replayed_refutations(run: ReviewRun) -> list[Refutation]:
    """Return the refutations ``run`` stored."""
    return [refutation_from_json(item) for item in run.payload.get("refutations", [])]


def replayed_spec(run: ReviewRun) -> dict[str, object]:
    """Return the spec fields ``run`` stored."""
    return dict(run.payload.get("spec", {}))


def cache_line(run: ReviewRun) -> str:
    """Return the stderr line a replayed review prints."""
    return (
        f"review: cached at {run.head_sha[:7]} (run {run.run_id}, {run.completed_at}); {FORCE_HINT}"
    )
