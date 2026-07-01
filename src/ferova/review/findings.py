"""Finding model, claim taxonomy, lifecycle law, and pr_findings ledger.

Redesign principle: every reviewer observation is a structured Finding that
travels through a well-defined lifecycle (proposed -> verified/refuted ->
open -> resolved/stuck).  The lifecycle is enforced by a single source-of-truth
transition table (ALLOWED_TRANSITIONS) so all agents -- reviewer, verifier,
coder -- speak the same state machine.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine

from ..core.logging import get_logger

logger = get_logger(__name__)

_metadata = MetaData()


class ClaimType(StrEnum):
    """Type of claim raised by a reviewer."""

    MISSING_TEST = "missing_test"
    MISSING_DOCSTRING = "missing_docstring"
    LINT_CONVENTION = "lint_convention"
    BROKEN_BEHAVIOR = "broken_behavior"
    SPEC_GAP = "spec_gap"
    DESIGN = "design"
    SECURITY = "security"


class Severity(StrEnum):
    """Severity level of a finding."""

    BLOCKING = "blocking"
    ADVISORY = "advisory"


class FindingStatus(StrEnum):
    """Lifecycle state of a finding."""

    PROPOSED = "proposed"
    VERIFIED = "verified"
    REFUTED = "refuted"
    OPEN = "open"
    RESOLVED = "resolved"
    STUCK = "stuck"


ALLOWED_TRANSITIONS: dict[FindingStatus, frozenset[FindingStatus]] = {
    FindingStatus.PROPOSED: frozenset({FindingStatus.VERIFIED, FindingStatus.REFUTED}),
    FindingStatus.VERIFIED: frozenset({FindingStatus.OPEN}),
    FindingStatus.OPEN: frozenset({FindingStatus.RESOLVED, FindingStatus.STUCK}),
    FindingStatus.REFUTED: frozenset(),
    FindingStatus.RESOLVED: frozenset(),
    FindingStatus.STUCK: frozenset(),
}


def is_valid_transition(src: FindingStatus, dst: FindingStatus) -> bool:
    """Return True if moving from src to dst is a legal lifecycle transition.

    Args:
        src: Current status of the finding.
        dst: Desired next status.

    Returns:
        True when the transition is permitted by ALLOWED_TRANSITIONS.
    """
    return dst in ALLOWED_TRANSITIONS.get(src, frozenset())


class Finding(BaseModel):
    """A structured observation raised by a reviewer agent.

    Args:
        pr_number: GitHub pull-request number this finding belongs to.
        head_sha: Commit SHA at the time the finding was raised.
        round: Review iteration index (starts at 1).
        finder: Identifier of the agent that raised the finding.
        claim_type: Taxonomy bucket for this observation.
        severity: Whether this finding must be resolved before merge.
        file: Repo-relative path of the file containing the issue.
        line_start: First line of the affected range (1-indexed).
        line_end: Last line of the affected range (inclusive).
        claim: Human-readable description of the problem.
        evidence_pointer: Opaque reference to supporting evidence.
        status: Current lifecycle state; defaults to PROPOSED.
        verification_method: How the verifier checked this finding.
        verification_result: What the verifier concluded.
        checked_at_sha: Commit SHA when the finding was last verified.
        id: Database surrogate key; None until persisted.
    """

    pr_number: int
    head_sha: str
    round: int
    finder: str
    claim_type: ClaimType
    severity: Severity
    file: str
    line_start: int
    line_end: int
    claim: str
    evidence_pointer: str
    status: FindingStatus = FindingStatus.PROPOSED
    verification_method: str = ""
    verification_result: str = ""
    checked_at_sha: str = ""
    id: int | None = None


pr_findings = Table(
    "pr_findings",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("pr_number", Integer, nullable=False),
    Column("head_sha", String, nullable=False),
    Column("round", Integer, nullable=False),
    Column("finder", String, nullable=False),
    Column("claim_type", String, nullable=False),
    Column("severity", String, nullable=False),
    Column("file", String, nullable=False),
    Column("line_start", Integer, nullable=False),
    Column("line_end", Integer, nullable=False),
    Column("claim", String, nullable=False),
    Column("evidence_pointer", String, nullable=False),
    Column("status", String, nullable=False),
    Column("verification_method", String, nullable=False),
    Column("verification_result", String, nullable=False),
    Column("checked_at_sha", String, nullable=False),
)


pr_review_integrity = Table(
    "pr_review_integrity",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("pr_number", Integer, nullable=False),
    Column("head_sha", String, nullable=False),
    Column("n_reviewers", Integer, nullable=False),
    Column("n_unparsed", Integer, nullable=False),
)


def _engine_for(db_path: Path) -> Engine:
    """Return a SQLite engine, creating the parent directory if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{db_path}")


def init_findings_schema(db_path: Path) -> None:
    """Create the findings + review-integrity tables if absent (idempotent)."""
    engine = _engine_for(db_path)
    _metadata.create_all(engine, checkfirst=True)


def record_review_integrity(
    db_path: Path, *, pr_number: int, head_sha: str | None, n_reviewers: int, n_unparsed: int
) -> None:
    """Record that the bench ran on a head, with how many outcomes were unparsed.

    The pure merge gate (SP-PURE-MERGE-GATE) requires a fresh, complete
    review (all reviewers parsed, zero unparsed) at the merge head — this
    is the evidence-first replacement for the forgeable archive verdict
    and the close of the parse_failed-promote hole (audit CRITICAL #2).

    Args:
        db_path: The findings ledger database.
        pr_number: The PR reviewed.
        head_sha: The head the bench ran on (``None`` becomes ``""``).
        n_reviewers: How many reviewers produced an outcome.
        n_unparsed: How many of those were unparsed (transport / crash).
    """
    init_findings_schema(db_path)
    engine = _engine_for(db_path)
    with engine.begin() as conn:
        conn.execute(
            insert(pr_review_integrity).values(
                pr_number=pr_number,
                head_sha=head_sha or "",
                n_reviewers=n_reviewers,
                n_unparsed=n_unparsed,
            )
        )


def fetch_review_integrity(db_path: Path, pr_number: int) -> list[dict[str, object]]:
    """Return the review-integrity records for a PR, ordered by id.

    Args:
        db_path: The findings ledger database.
        pr_number: The PR to fetch records for.

    Returns:
        One dict per record with ``head_sha`` / ``n_reviewers`` /
        ``n_unparsed`` keys, oldest first.
    """
    init_findings_schema(db_path)
    engine = _engine_for(db_path)
    stmt = (
        select(pr_review_integrity)
        .where(pr_review_integrity.c.pr_number == pr_number)
        .order_by(pr_review_integrity.c.id)
    )
    with engine.connect() as conn:
        rows = list(conn.execute(stmt).mappings())
    return [
        {
            "head_sha": row["head_sha"],
            "n_reviewers": row["n_reviewers"],
            "n_unparsed": row["n_unparsed"],
        }
        for row in rows
    ]


def record_finding(db_path: Path, finding: Finding) -> int:
    """Insert a Finding and return its new surrogate id."""
    engine = _engine_for(db_path)
    stmt = insert(pr_findings).values(
        pr_number=finding.pr_number,
        head_sha=finding.head_sha,
        round=finding.round,
        finder=finding.finder,
        claim_type=str(finding.claim_type),
        severity=str(finding.severity),
        file=finding.file,
        line_start=finding.line_start,
        line_end=finding.line_end,
        claim=finding.claim,
        evidence_pointer=finding.evidence_pointer,
        status=str(finding.status),
        verification_method=finding.verification_method,
        verification_result=finding.verification_result,
        checked_at_sha=finding.checked_at_sha,
    )
    with engine.connect() as conn:
        result = conn.execute(stmt)
        conn.commit()
        return result.inserted_primary_key[0]


def update_finding_status(
    db_path: Path,
    finding_id: int,
    new_status: FindingStatus,
    *,
    verification_method: str = "",
    verification_result: str = "",
    checked_at_sha: str = "",
) -> bool:
    """Transition a finding to a new lifecycle status.

    Reads the current status and validates via is_valid_transition.  Emits a
    findings.invalid_transition structlog warning and returns False on rejection.

    Args:
        db_path: Path to the SQLite database file.
        finding_id: Surrogate key of the row to update.
        new_status: Target lifecycle status.
        verification_method: How the verifier checked this finding.
        verification_result: What the verifier concluded.
        checked_at_sha: Commit SHA when the finding was last verified.

    Returns:
        True on success; False when the transition is illegal or row not found.
    """
    engine = _engine_for(db_path)
    with engine.connect() as conn:
        result = conn.execute(select(pr_findings).where(pr_findings.c.id == finding_id))
        row = result.mappings().fetchone()
        if row is None:
            return False
        current_status = FindingStatus(row["status"])
        if not is_valid_transition(current_status, new_status):
            logger.warning(
                "findings.invalid_transition",
                finding_id=finding_id,
                current_status=current_status,
                new_status=new_status,
            )
            return False
        conn.execute(
            update(pr_findings)
            .where(pr_findings.c.id == finding_id)
            .values(
                status=str(new_status),
                verification_method=verification_method,
                verification_result=verification_result,
                checked_at_sha=checked_at_sha,
            )
        )
        conn.commit()
        return True


def _rows_to_findings(rows: list) -> list[Finding]:
    """Materialise pr_findings mapping rows into :class:`Finding` models."""
    return [
        Finding(
            id=row["id"],
            pr_number=int(row["pr_number"]),
            head_sha=row["head_sha"],
            round=int(row["round"]),
            finder=row["finder"],
            claim_type=ClaimType(row["claim_type"]),
            severity=Severity(row["severity"]),
            file=row["file"],
            line_start=int(row["line_start"]),
            line_end=int(row["line_end"]),
            claim=row["claim"],
            evidence_pointer=row["evidence_pointer"],
            status=FindingStatus(row["status"]),
            verification_method=row["verification_method"],
            verification_result=row["verification_result"],
            checked_at_sha=row["checked_at_sha"],
        )
        for row in rows
    ]


def fetch_all_findings(db_path: Path) -> list[Finding]:
    """Return every recorded finding across all PRs, ordered by id.

    Feeds the aggregate insights report (SP-REVIEW-LESSONS) — per-lens
    precision and status/claim-type distributions over the whole ledger.
    """
    init_findings_schema(db_path)
    engine = _engine_for(db_path)
    stmt = select(pr_findings).order_by(pr_findings.c.id)
    with engine.connect() as conn:
        rows = conn.execute(stmt).mappings().fetchall()
    return _rows_to_findings(rows)


def fetch_findings(
    db_path: Path,
    pr_number: int,
    *,
    status: FindingStatus | None = None,
) -> list[Finding]:
    """Return findings for a PR, optionally filtered by status, ordered by id."""
    engine = _engine_for(db_path)
    stmt = select(pr_findings).where(pr_findings.c.pr_number == pr_number)
    if status is not None:
        stmt = stmt.where(pr_findings.c.status == str(status))
    stmt = stmt.order_by(pr_findings.c.id)
    with engine.connect() as conn:
        rows = conn.execute(stmt).mappings().fetchall()
    return _rows_to_findings(rows)
