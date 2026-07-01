"""Per-model reviewer precision harvest (SP-CHAINPILOT-REVIEWER-OUTCOMES).

Phase 2c of the Chain Autopilot arc — the second half of the live performance
harvest (brick 4). It re-attributes slice 11's confirmed-vs-refuted finding
signal **per model**: the fraction of the findings a model raised that survived
verification rather than being refuted. The reviewer counterpart to 2b's coder
outcomes; together they are the live posterior (Phase 2d) that overrides the
benchmark prior.

The model is not on the ``Finding`` (which carries ``finder`` = the lens role);
it is recovered from ``pr_reviews``, which stores ``model_used`` per
``(pr_number, role)``. A finding joins to its model via
``(finding.pr_number, finding.finder) == (pr_reviews.pr_number,
pr_reviews.role)`` — ``findings_bridge`` sets ``finder = role.value``, the same
lowercase role ``pr_reviews.role`` holds. CI-authored findings
(``finder == "ci"``) and any finding with no matching review row are
unattributable and excluded. A ``(pr, role)`` mapping to several models across
review rounds contributes the finding to each. No minimum-sample guard is
applied here; that is Phase 2d.

This leaf lives in ``review/`` rather than beside the other arc bricks in
``llm_proxy/providers/`` so the one-way ``review -> llm_proxy`` import boundary
is preserved.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from .findings import FindingStatus, fetch_all_findings, init_findings_schema
from .persistence import _engine_for, _pr_reviews, init_schema

_CONFIRMED_REAL: frozenset[FindingStatus] = frozenset(
    {
        FindingStatus.VERIFIED,
        FindingStatus.OPEN,
        FindingStatus.RESOLVED,
        FindingStatus.STUCK,
    }
)
"""Statuses downstream of VERIFIED — the finding was confirmed a real problem.

Mirrors slice 11's ``review_lessons._CONFIRMED_REAL`` so the per-model precision
here matches the per-lens precision there.
"""

__all__ = ["ReviewerModelOutcome", "harvest_reviewer_outcomes"]


@dataclass(frozen=True)
class ReviewerModelOutcome:
    """One reviewer model's confirmed-vs-refuted track record.

    Attributes:
        model: The ``pr_reviews.model_used`` identifier.
        confirmed: Findings the model raised that are downstream of VERIFIED.
        refuted: Findings the model raised that the verifier refuted.
        n_settled: ``confirmed + refuted`` — findings that reached a verdict.
        precision: ``confirmed / n_settled``, or ``None`` when the model has no
            settled finding yet.
    """

    model: str
    confirmed: int
    refuted: int
    n_settled: int
    precision: float | None


def harvest_reviewer_outcomes(db_path: Path) -> list[ReviewerModelOutcome]:
    """Aggregate per-model reviewer precision from the findings ledger.

    Pure and read-only: the idempotent ``init_*`` helpers ensure the tables
    exist (so a fresh database yields ``[]`` rather than raising), then a single
    pass joins each finding to the model(s) that produced its review and tallies
    confirmed vs refuted.

    Args:
        db_path: The review ledger SQLite database.

    Returns:
        One :class:`ReviewerModelOutcome` per attributed model, ordered by model
        id.
    """
    init_schema(db_path)
    init_findings_schema(db_path)
    engine = _engine_for(db_path)
    with engine.connect() as conn:
        review_rows = conn.execute(
            select(_pr_reviews.c.pr_number, _pr_reviews.c.role, _pr_reviews.c.model_used)
        ).all()

    models_by_pr_role: dict[tuple[int, str], set[str]] = defaultdict(set)
    for pr_number, role, model_used in review_rows:
        if model_used:
            models_by_pr_role[(pr_number, role)].add(model_used)

    confirmed: dict[str, int] = defaultdict(int)
    refuted: dict[str, int] = defaultdict(int)
    seen: set[str] = set()
    for finding in fetch_all_findings(db_path):
        models = models_by_pr_role.get((finding.pr_number, finding.finder))
        if not models:
            continue
        if finding.status in _CONFIRMED_REAL:
            bucket: dict[str, int] | None = confirmed
        elif finding.status is FindingStatus.REFUTED:
            bucket = refuted
        else:
            bucket = None
        for model in models:
            seen.add(model)
            if bucket is not None:
                bucket[model] += 1

    outcomes: list[ReviewerModelOutcome] = []
    for model in sorted(seen):
        c = confirmed[model]
        r = refuted[model]
        n_settled = c + r
        outcomes.append(
            ReviewerModelOutcome(
                model=model,
                confirmed=c,
                refuted=r,
                n_settled=n_settled,
                precision=(c / n_settled) if n_settled else None,
            )
        )
    return outcomes
