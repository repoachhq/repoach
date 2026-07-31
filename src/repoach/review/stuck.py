"""Slice 9 — cross-run stuck detection + escalation for the findings Coder.

The findings-driven Coder loop is driven by GitHub's push -> CI ->
re-review cycle: every ``review fix`` that pushes a commit retriggers a
fresh review run that re-invokes the Coder. The legacy archive-verdict
Coder bounded that with ``MAX_ITERATIONS``; SP-DELETE-LEGACY-CODER deleted
it and deferred the replacement to this slice.

This module supplies the bound. A small ``pr_coder_rounds`` ledger records
the open-blocking-finding count before and after each Coder round, so
progress is measurable across the separate CI runs (the row survives
between jobs inside the findings-ledger artifact). :func:`assess_stuck` is
a pure function over that history: it declares a PR stuck when the
iteration cap is reached (parity with the legacy cap) or when no progress
has been made over a recent window of rounds. :func:`mark_findings_stuck`
then drives the surviving open blocking findings into the terminal
``stuck`` state and :func:`build_stuck_dossier` assembles the operator
escalation payload. Firing the payload (settings + network) is the
caller's job so this module stays pure and unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    create_engine,
    insert,
    select,
)
from sqlalchemy.engine import Engine

from ..core.logging import get_logger
from ..core.sqlite_schema_init import ensure_schema_created
from .findings import Finding, FindingStatus, Severity, fetch_findings, update_finding_status

_log = get_logger(__name__)

_metadata = MetaData()

MAX_CODER_ROUNDS = 3
"""Hard cap on Coder rounds per PR (parity with the legacy ``MAX_ITERATIONS``).

Three fix attempts are allowed; the fourth start-check escalates instead.
"""

STALL_WINDOW = 2
"""Number of trailing rounds inspected for the no-progress stall signal.

A PR that fails to reduce its open-blocking count across this many rounds
escalates one round before the hard cap, sparing a wasted attempt.
"""

PROPOSED_ESCALATION_ATTEMPTS: int = 2
"""Number of verification attempts after which a still-proposed blocking
finding is escalated to the operator dossier.

Two attempts are allowed; the third run's :func:`assess_proposed_escalation`
names the finding for escalation, and :func:`select_newly_escalated` fires
the dossier exactly once at the crossing.
"""


pr_coder_rounds = Table(
    "pr_coder_rounds",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("pr_number", Integer, nullable=False),
    Column("round_index", Integer, nullable=False),
    Column("open_blocking_before", Integer, nullable=False),
    Column("open_blocking_after", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


@dataclass(frozen=True)
class CoderRound:
    """One recorded Coder round's progress on a PR.

    Attributes:
        round_index: 1-based position of this round for the PR.
        open_blocking_before: Open blocking findings the round started with.
        open_blocking_after: Open blocking findings surviving re-verification.
    """

    round_index: int
    open_blocking_before: int
    open_blocking_after: int


@dataclass(frozen=True)
class StuckAssessment:
    """The verdict of :func:`assess_stuck` over a PR's round history.

    Attributes:
        stuck: Whether the loop should escalate instead of fixing again.
        reason: Human-readable cause; empty when not stuck.
        n_rounds: How many prior rounds were considered.
    """

    stuck: bool
    reason: str
    n_rounds: int


def _engine_for(db_path: Path) -> Engine:
    """Return a SQLite engine, creating the parent directory if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{db_path}")


def init_stuck_schema(db_path: Path) -> None:
    """Create the ``pr_coder_rounds`` table if absent (idempotent)."""
    engine = _engine_for(db_path)
    ensure_schema_created(engine, _metadata)


def record_coder_round(
    db_path: Path,
    *,
    pr_number: int,
    open_blocking_before: int,
    open_blocking_after: int,
) -> int:
    """Append one round row for a PR and return its 1-based ``round_index``.

    Args:
        db_path: The findings ledger database.
        pr_number: The PR the round belongs to.
        open_blocking_before: Open blocking findings at the round's start.
        open_blocking_after: Open blocking findings surviving re-verification.

    Returns:
        The 1-based index of the recorded round.
    """
    init_stuck_schema(db_path)
    engine = _engine_for(db_path)
    with engine.begin() as conn:
        prior = conn.execute(
            select(pr_coder_rounds.c.id).where(pr_coder_rounds.c.pr_number == pr_number)
        ).fetchall()
        round_index = len(prior) + 1
        conn.execute(
            insert(pr_coder_rounds).values(
                pr_number=pr_number,
                round_index=round_index,
                open_blocking_before=open_blocking_before,
                open_blocking_after=open_blocking_after,
                created_at=datetime.now(UTC),
            )
        )
    return round_index


def fetch_coder_rounds(db_path: Path, pr_number: int) -> list[CoderRound]:
    """Return a PR's recorded Coder rounds, oldest first."""
    init_stuck_schema(db_path)
    engine = _engine_for(db_path)
    stmt = (
        select(pr_coder_rounds)
        .where(pr_coder_rounds.c.pr_number == pr_number)
        .order_by(pr_coder_rounds.c.id)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).mappings().fetchall()
    return [
        CoderRound(
            round_index=int(row["round_index"]),
            open_blocking_before=int(row["open_blocking_before"]),
            open_blocking_after=int(row["open_blocking_after"]),
        )
        for row in rows
    ]


def assess_stuck(
    rounds: list[CoderRound],
    *,
    max_rounds: int = MAX_CODER_ROUNDS,
    stall_window: int = STALL_WINDOW,
) -> StuckAssessment:
    """Decide whether a PR's Coder loop is stuck, purely from its history.

    The loop is stuck when the iteration cap is reached, or when the most
    recent ``stall_window`` rounds show a non-decreasing (still positive)
    open-blocking count — no progress. A strictly decreasing trailing
    window is progress, so only the hard cap stops a slowly-improving PR.

    Args:
        rounds: The PR's prior rounds, oldest first.
        max_rounds: Cap on rounds before forced escalation.
        stall_window: Trailing rounds inspected for the stall signal.

    Returns:
        A :class:`StuckAssessment`.
    """
    n = len(rounds)
    if n == 0:
        return StuckAssessment(stuck=False, reason="", n_rounds=0)
    if n >= max_rounds:
        return StuckAssessment(
            stuck=True, reason=f"iteration cap reached ({n}/{max_rounds})", n_rounds=n
        )
    if n >= stall_window:
        afters = [r.open_blocking_after for r in rounds[-stall_window:]]
        non_decreasing = all(afters[i] >= afters[i - 1] for i in range(1, len(afters)))
        if afters[-1] > 0 and non_decreasing:
            return StuckAssessment(
                stuck=True, reason=f"no progress over last {stall_window} rounds", n_rounds=n
            )
    return StuckAssessment(stuck=False, reason="", n_rounds=n)


def mark_findings_stuck(db_path: Path, pr_number: int) -> list[Finding]:
    """Drive a PR's open blocking findings into the terminal ``stuck`` state.

    Only ``open`` blocking findings are transitioned (``open -> stuck`` is
    the sole legal route into the state); the original verification
    evidence is preserved and the result records the escalation.

    Args:
        db_path: The findings ledger database.
        pr_number: The PR whose open blocking findings to escalate.

    Returns:
        The findings transitioned to ``stuck``.
    """
    marked: list[Finding] = []
    for finding in fetch_findings(db_path, pr_number, status=FindingStatus.OPEN):
        if finding.severity is not Severity.BLOCKING or finding.id is None:
            continue
        if update_finding_status(
            db_path,
            finding.id,
            FindingStatus.STUCK,
            verification_method=finding.verification_method,
            verification_result="auto-resolution exhausted — escalated",
            checked_at_sha=finding.checked_at_sha,
        ):
            marked.append(finding)
    if marked:
        _log.warning("stuck.findings_marked", pr_number=pr_number, n_stuck=len(marked))
    return marked


def build_stuck_dossier(
    *,
    pr_number: int,
    reason: str,
    rounds: list[CoderRound],
    stuck_findings: list[Finding],
) -> dict[str, object]:
    """Assemble the operator escalation payload for a stuck PR.

    Args:
        pr_number: The stuck PR.
        reason: Why it is stuck (cap or stall).
        rounds: The PR's recorded round history.
        stuck_findings: The findings just driven to ``stuck``.

    Returns:
        A small JSON-serialisable dict; ``kind`` discriminates it from the
        per-round TeamOutcome ping the orchestrator already fires.
    """
    return {
        "kind": "stuck_escalation",
        "pr_number": pr_number,
        "reason": reason,
        "n_rounds": len(rounds),
        "trajectory": [
            {
                "round": r.round_index,
                "before": r.open_blocking_before,
                "after": r.open_blocking_after,
            }
            for r in rounds
        ],
        "stuck_findings": [
            {
                "finder": f.finder,
                "claim_type": str(f.claim_type),
                "file": f.file,
                "line": f.line_start,
                "claim": f.claim,
            }
            for f in stuck_findings
        ],
    }


def assess_proposed_escalation(
    findings: list[Finding],
    *,
    max_attempts: int = PROPOSED_ESCALATION_ATTEMPTS,
) -> list[Finding]:
    """Return blocking findings still PROPOSED whose verify_attempts >= max_attempts.

    Pure function over the findings list. A finding qualifies when it is
    blocking, still proposed, and has accrued at least ``max_attempts``
    verification attempts (deferrals or otherwise). The assessor is the
    read-side companion to :func:`select_newly_escalated`, which fires
    the dossier exactly once at the crossing.

    Args:
        findings: The findings to assess.
        max_attempts: Minimum ``verify_attempts`` count for escalation.

    Returns:
        The blocking proposed findings eligible for escalation.
    """
    return [
        f
        for f in findings
        if f.severity is Severity.BLOCKING
        and f.status is FindingStatus.PROPOSED
        and f.verify_attempts >= max_attempts
    ]


def select_newly_escalated(
    findings: list[Finding],
    *,
    max_attempts: int = PROPOSED_ESCALATION_ATTEMPTS,
) -> list[Finding]:
    """Return findings whose verify_attempts just crossed ``max_attempts``.

    The just-crossed set: a finding with ``verify_attempts == max_attempts``
    is included; one with ``verify_attempts == max_attempts + 1`` is not.
    This ensures the dossier fires exactly once per finding across
    successive runs.

    Args:
        findings: The findings to filter.
        max_attempts: The threshold for "just crossed".

    Returns:
        The findings that crossed the threshold on this run.
    """
    return [
        f
        for f in findings
        if f.severity is Severity.BLOCKING
        and f.status is FindingStatus.PROPOSED
        and f.verify_attempts == max_attempts
    ]


def build_proposed_escalation_dossier(
    findings: list[Finding],
) -> dict[str, object]:
    """Assemble the operator escalation payload for proposed findings.

    Mirrors :func:`build_stuck_dossier` but for findings frozen in
    ``proposed`` after repeated verification deferrals. The dossier
    carries the deferral diagnostic (verification_method +
    verification_result) so the operator inherits a named, evidenced
    decision instead of a silent forever-refusal.

    Args:
        findings: The blocking proposed findings to escalate.

    Returns:
        A small JSON-serialisable dict; ``kind`` discriminates it from
        the ``stuck_escalation`` dossier the orchestrator already fires.
    """
    return {
        "kind": "proposed_escalation",
        "findings": [
            {
                "id": f.id,
                "claim": f.claim,
                "claim_type": str(f.claim_type),
                "verify_attempts": f.verify_attempts,
                "verification_method": f.verification_method,
                "verification_result": f.verification_result,
            }
            for f in findings
        ],
    }
