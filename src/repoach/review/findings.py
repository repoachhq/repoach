"""Finding model, claim taxonomy, lifecycle law, and pr_findings ledger.

Redesign principle: every reviewer observation is a structured Finding that
travels through a well-defined lifecycle (proposed -> verified/refuted ->
open -> resolved/stuck).  The lifecycle is enforced by a single source-of-truth
transition table (ALLOWED_TRANSITIONS) so all agents -- reviewer, verifier,
coder -- speak the same state machine.

REFUTED is re-openable (SP-REFUTER-INJECTION-HARDEN): refuted ->
proposed is admitted so a later reviewer re-raising the same claim
sends the finding back for a fresh judging round.  Without that exit a
single injected refutation verdict would bury a real blocking finding
forever, self-defended by the refuted-finding sentinel.  RESOLVED and
STUCK stay terminal.
"""

from __future__ import annotations

import threading
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
    inspect,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

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


JUDGED_CLAIM_TYPES = frozenset({ClaimType.DESIGN, ClaimType.SECURITY, ClaimType.SPEC_GAP})
"""Claim types too subjective for a mechanical on-disk check — the
refuter adversarially judges them instead (SP-REFUTER). The single
source of truth for "judged" membership (SP-CLAIM-TYPE-PARTITION-ALIGN):
:mod:`refuter` and :mod:`merge_gate` both import this constant rather
than keeping their own copies, so a claim type the refuter judges can
never silently fall outside what the merge gate counts as an open
blocking finding.
"""

MECHANICAL_CLAIM_TYPES = frozenset(
    {ClaimType.MISSING_TEST, ClaimType.MISSING_DOCSTRING, ClaimType.LINT_CONVENTION}
)
"""Claim types resolved by a mechanical on-disk re-check rather than
adversarial judging. The single source of truth (mirrors
JUDGED_CLAIM_TYPES): merge_gate and coder_findings both import this
constant rather than keeping their own copies."""


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


CONFIRMED_REAL_STATUSES: frozenset[FindingStatus] = frozenset(
    {FindingStatus.VERIFIED, FindingStatus.OPEN, FindingStatus.RESOLVED, FindingStatus.STUCK}
)
"""Statuses downstream of VERIFIED — the finding was confirmed a real
problem. The single source of truth: reviewer_outcomes and
review_lessons both import this constant rather than keeping their own
copies."""


ALLOWED_TRANSITIONS: dict[FindingStatus, frozenset[FindingStatus]] = {
    FindingStatus.PROPOSED: frozenset({FindingStatus.VERIFIED, FindingStatus.REFUTED}),
    FindingStatus.VERIFIED: frozenset({FindingStatus.OPEN}),
    FindingStatus.OPEN: frozenset({FindingStatus.RESOLVED, FindingStatus.STUCK}),
    FindingStatus.REFUTED: frozenset({FindingStatus.PROPOSED}),
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
    verify_attempts: int = 0
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
    Column("verify_attempts", Integer, nullable=False, default=0),
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


_INIT_SCHEMA_LOCK = threading.Lock()
"""Serializes concurrent first-creation within one process.

``create_all(checkfirst=True)`` builds ``pr_findings`` and
``pr_review_integrity`` sequentially, so two in-process threads racing
the very first creation can interleave: the loser's ``CREATE
pr_findings`` fails against the winner's already-committed table, and
if the loser's failure is observed before the winner has gone on to
create ``pr_review_integrity``, a re-check of *both* tables at that
instant still reports one missing. Holding this lock around the whole
``create_all`` call makes every in-process caller either run the
sequence to completion before the next one starts, or find both
tables already there and no-op through ``checkfirst`` -- turning the
in-process race into deterministic serialization rather than relying
on retries to paper over interleavings. Cross-process races (separate
SQLite connections in separate interpreters, which no in-process lock
can reach) are still handled by :func:`_create_all_with_retries`.
"""

_INIT_SCHEMA_MAX_ATTEMPTS = 5
"""Bound on ``create_all`` retries in :func:`_create_all_with_retries`.

Tables are only ever added, never dropped, so each retry's
``checkfirst=True`` skips whatever the previous attempt (or a racing
process) already committed and creates only what is still missing --
the sequence converges to a no-op within a handful of attempts. Five
is comfortably above the two-table depth of this schema; it exists
only to keep a genuine, persistent failure (e.g. an unwritable
database file) from retrying forever.
"""


def _create_all_with_retries(engine: Engine) -> None:
    """Run ``create_all(checkfirst=True)`` to convergence across racing creators.

    A losing ``CREATE TABLE`` from a racing SQLite connection surfaces
    as ``OperationalError`` even though SQLite DDL is transactional
    and never leaves a half-built table behind. Retrying re-enters
    ``checkfirst=True``, which skips every table that now exists
    (whichever process created it) and creates only what is still
    missing, so at most one retry per remaining table is needed before
    both are present. The bounded loop re-raises the last
    ``OperationalError`` only if, after exhausting
    :data:`_INIT_SCHEMA_MAX_ATTEMPTS`, at least one of the two tables
    genuinely never came to exist -- the signal that this was a real
    database failure rather than a resolved creation race.

    Args:
        engine: SQLAlchemy engine bound to the findings database.

    Raises:
        OperationalError: The database remains unusable after every
            retry -- both tables still absent.
    """
    last_error: OperationalError | None = None
    for attempt in range(_INIT_SCHEMA_MAX_ATTEMPTS):
        try:
            _metadata.create_all(engine, checkfirst=True)
            return
        except OperationalError as exc:
            last_error = exc
            logger.info(
                "review.track_record.schema_init_retry",
                attempt=attempt,
                error=str(exc),
            )
    inspector = inspect(engine)
    if inspector.has_table("pr_findings") and inspector.has_table("pr_review_integrity"):
        return
    assert last_error is not None
    raise last_error


def init_findings_schema(db_path: Path) -> None:
    """Create the findings + review-integrity tables if absent (idempotent).

    Concurrent first-creation is race-proof at two layers. In-process
    callers (e.g. the reviewer thread pool) serialize on
    :data:`_INIT_SCHEMA_LOCK` so ``create_all`` runs to completion
    before the next caller starts, or finds both tables already there
    and no-ops. Cross-process callers (separate SQLite connections
    with no shared lock) are handled by
    :func:`_create_all_with_retries`, which retries the bounded,
    convergent ``checkfirst=True`` sequence and re-raises only a
    genuine, persistent failure -- one where a table never comes to
    exist even after every retry.

    Also self-heals existing databases by ALTER-ing columns introduced
    post-creation (SQLite has no DDL-versioning).
    """
    engine = _engine_for(db_path)
    with _INIT_SCHEMA_LOCK:
        _create_all_with_retries(engine)
    _migrate_missing_findings_columns(engine)


def _migrate_missing_findings_columns(engine: Engine) -> None:
    """Add columns introduced post-creation to older findings databases.

    Each entry is a single ``ALTER TABLE`` documented with the spec that
    introduced it.  Idempotent -- checks ``has_column`` first.

    Args:
        engine: SQLAlchemy engine bound to the findings database.
    """
    from sqlalchemy import text

    migrations = (
        (
            "pr_findings",
            "verify_attempts",
            "INTEGER NOT NULL DEFAULT 0",
            "SP-PROPOSED-ESCALATION 1/5",
        ),
    )

    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, column, ddl, _release in migrations:
            if not inspector.has_table(table):
                continue
            existing = {col["name"] for col in inspector.get_columns(table)}
            if column in existing:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


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
        verify_attempts=finding.verify_attempts,
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


def record_verification_attempt(
    db_path: Path,
    finding_id: int,
    *,
    method: str,
    result: str,
    checked_at_sha: str,
) -> int:
    """Record a verification attempt on a finding without changing its status.

    Persists the verifier's diagnostic (method + result + checked_at_sha),
    increments ``verify_attempts`` by one, and returns the new attempt count.
    The lifecycle status is NEVER touched -- a deferred verification stays
    ``proposed`` so the judge fallback (SP-PROPOSED-ESCALATION 3/5) can pick
    it up in the same run.

    Args:
        db_path: Path to the SQLite database file.
        finding_id: Surrogate key of the row to update.
        method: How the verifier checked this finding (e.g. ``"symbol_search"``).
        result: What the verifier concluded (e.g. ``"no checkable symbol"``).
        checked_at_sha: Commit SHA when the finding was last verified.

    Returns:
        The new ``verify_attempts`` count after the increment. Returns ``0``
        when the row does not exist.
    """
    init_findings_schema(db_path)
    engine = _engine_for(db_path)
    with engine.begin() as conn:
        row = (
            conn.execute(
                select(pr_findings.c.verify_attempts).where(pr_findings.c.id == finding_id)
            )
            .mappings()
            .fetchone()
        )
        if row is None:
            return 0
        new_count = int(row["verify_attempts"]) + 1
        conn.execute(
            update(pr_findings)
            .where(pr_findings.c.id == finding_id)
            .values(
                verification_method=method,
                verification_result=result,
                checked_at_sha=checked_at_sha,
                verify_attempts=new_count,
            )
        )
    return new_count


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
            verify_attempts=int(row["verify_attempts"]),
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
