"""Per-model Coder outcome harvest (SP-CHAINPILOT-CODER-OUTCOMES).

Phase 2b of the Chain Autopilot arc — the first half of the live performance
harvest (brick 4 of the architecture). Aggregates the factory's own Coder
results per model into the evidence the chain-autopilot posterior (Phase 2d)
uses to override the benchmark prior: how often each Coder model lands green
CI, how many rounds it takes, and how often it gets stuck.

The harvest is pure and read-only over four existing review tables:
``pr_coder_responses`` carries the ``model_used`` attribution key,
``pr_merges`` records the terminal green-CI signal, ``pr_coder_rounds`` holds
the per-round progress, and ``pr_findings`` the terminal ``STUCK`` state.
``model_used`` lives only on ``pr_coder_responses``, so a PR's rounds and
outcome attribute to the Coder model(s) that served it — the single chain-head
model in the common case, or every model seen on a mid-PR failover. No
minimum-sample guard is applied here; that is Phase 2d's responsibility.

This leaf lives in ``review/`` rather than beside the other arc bricks in
``llm_proxy/providers/`` so the one-way ``review -> llm_proxy`` import boundary
is preserved: reading these tables from ``llm_proxy`` would invert it into a
cycle the edge-honesty gate rejects.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select

from .findings import FindingStatus, Severity, init_findings_schema, pr_findings
from .persistence import _engine_for, _pr_coder_responses, _pr_merges, init_schema
from .stuck import init_stuck_schema, pr_coder_rounds

_CI_GREEN_OUTCOMES: frozenset[str] = frozenset({"APPROVE", "ALREADY_MERGED"})
"""``pr_merges.outcome`` tags meaning the PR reached green CI and merged.

Mirrors ``auto_merge.OUTCOME_MERGED`` / ``OUTCOME_ALREADY_MERGED``; held as
literals to keep this read-only harvest decoupled from the merge machinery
(gh subprocess wrappers) it would otherwise import.
"""


@dataclass(frozen=True)
class CoderModelOutcome:
    """One Coder model's aggregated track record across the PRs it served.

    Attributes:
        model: The ``pr_coder_responses.model_used`` identifier.
        n_prs: Distinct PRs attributed to the model.
        n_ci_green: Attributed PRs that reached a green-CI merge.
        ci_green_rate: ``n_ci_green / n_prs``, or ``None`` when ``n_prs`` is 0.
        avg_rounds_to_green: Mean Coder-round count over the model's green PRs,
            or ``None`` when the model has no green PR with a recorded round.
        n_stuck: Attributed PRs holding a terminal blocking ``STUCK`` finding.
        stuck_rate: ``n_stuck / n_prs``, or ``None`` when ``n_prs`` is 0.
    """

    model: str
    n_prs: int
    n_ci_green: int
    ci_green_rate: float | None
    avg_rounds_to_green: float | None
    n_stuck: int
    stuck_rate: float | None


def harvest_coder_outcomes(db_path: Path) -> list[CoderModelOutcome]:
    """Aggregate per-model Coder outcomes from the review ledger.

    Pure and read-only: the idempotent ``init_*`` helpers ensure the tables
    exist (so a fresh database yields ``[]`` rather than raising), then a
    single read pass groups outcomes by ``model_used``. A PR served by more
    than one model contributes its outcome to each.

    Args:
        db_path: The review ledger SQLite database.

    Returns:
        One :class:`CoderModelOutcome` per model, ordered by model id.
    """
    init_schema(db_path)
    init_stuck_schema(db_path)
    init_findings_schema(db_path)
    engine = _engine_for(db_path)
    with engine.connect() as conn:
        attribution = conn.execute(
            select(
                _pr_coder_responses.c.model_used,
                _pr_coder_responses.c.pr_number,
            ).distinct()
        ).all()
        green_prs = {
            row.pr_number
            for row in conn.execute(
                select(_pr_merges.c.pr_number).where(_pr_merges.c.outcome.in_(_CI_GREEN_OUTCOMES))
            ).all()
        }
        rounds_by_pr = {
            row.pr_number: int(row.n_rounds)
            for row in conn.execute(
                select(
                    pr_coder_rounds.c.pr_number,
                    func.count().label("n_rounds"),
                ).group_by(pr_coder_rounds.c.pr_number)
            ).all()
        }
        stuck_prs = {
            row.pr_number
            for row in conn.execute(
                select(pr_findings.c.pr_number)
                .where(pr_findings.c.status == FindingStatus.STUCK.value)
                .where(pr_findings.c.severity == Severity.BLOCKING.value)
                .distinct()
            ).all()
        }

    prs_by_model: dict[str, set[int]] = {}
    for model_used, pr_number in attribution:
        prs_by_model.setdefault(model_used, set()).add(pr_number)

    outcomes: list[CoderModelOutcome] = []
    for model in sorted(prs_by_model):
        prs = prs_by_model[model]
        n_prs = len(prs)
        green = prs & green_prs
        n_green = len(green)
        n_stuck = len(prs & stuck_prs)
        green_rounds = [rounds_by_pr[pr] for pr in green if pr in rounds_by_pr]
        avg_rounds = (sum(green_rounds) / len(green_rounds)) if green_rounds else None
        outcomes.append(
            CoderModelOutcome(
                model=model,
                n_prs=n_prs,
                n_ci_green=n_green,
                ci_green_rate=(n_green / n_prs) if n_prs else None,
                avg_rounds_to_green=avg_rounds,
                n_stuck=n_stuck,
                stuck_rate=(n_stuck / n_prs) if n_prs else None,
            )
        )
    return outcomes
