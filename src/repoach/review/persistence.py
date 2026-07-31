"""L4 persistence for review-bot outcomes.

Each reviewer pass writes one row to ``pr_reviews``; each Coder
fix-plan writes one row to ``pr_coder_responses``; each speaker turn
in the dialogue writes one row to ``pr_review_dialogue``.  Tables are
created idempotently via ``init_schema``-style ``create_all`` calls.

Schema vocabulary :
    - ``pr_review_dialogue.round`` is one of ``"1"`` (initial reviewer
      outcome), ``"2"`` (round-2 self-revision), or ``"challenge"``
      (Coder ACCEPT / CHALLENGE / DEFER record).
    - ``pr_review_dialogue.speaker`` is one of ``"architect"``,
      ``"sentinel"``, ``"tester"``, ``"scribe"``, ``"coder"``.
    - ``pr_merges.outcome`` is one of ``APPROVE``, ``ALREADY_MERGED``,
      ``SKIP_BASE``, ``SKIP_GATE``, ``SKIP_CI_RED`` (and the other
      ``SKIP_CI_*`` tags) or ``FAILED``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    insert,
    select,
)

from ..core.logging import get_logger
from ..core.sqlite_schema_init import ensure_schema_created
from .hallucination_guard import GuardEvent
from .reviewer import ReviewerOutcome

_log = get_logger(__name__)

_metadata = MetaData()

_pr_reviews = Table(
    "pr_reviews",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("pr_number", Integer, nullable=False),
    Column("role", String, nullable=False),
    Column("verdict", String, nullable=False),
    Column("summary", String, nullable=False),
    Column("n_comments", Integer, nullable=False),
    Column("n_blockers", Integer, nullable=False),
    Column("n_majors", Integer, nullable=False),
    Column("model_used", String, nullable=False),
    Column("elapsed_s", Float, nullable=False),
    Column("tokens_used", Integer, nullable=False),
    Column("comments_json", String, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("decision_pivot", String, nullable=True),
)

_pr_coder_responses = Table(
    "pr_coder_responses",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("pr_number", Integer, nullable=False),
    Column("n_fixes_planned", Integer, nullable=False),
    Column("commit_message", String, nullable=False),
    Column("summary", String, nullable=False),
    Column("model_used", String, nullable=False),
    Column("elapsed_s", Float, nullable=False),
    Column("tokens_used", Integer, nullable=False),
    Column("fixes_json", String, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

_pr_hallucinations = Table(
    "pr_hallucinations",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("pr_number", Integer, nullable=False),
    Column("role", String, nullable=False),
    Column("file", String, nullable=False),
    Column("line", Integer, nullable=False),
    Column("original_severity", String, nullable=False),
    Column("reason", String, nullable=False),
    Column("tokens_found", String, nullable=False),
    Column("original_body", String, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

_pr_review_dialogue = Table(
    "pr_review_dialogue",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("pr_number", Integer, nullable=False),
    Column("round", String, nullable=False),
    Column("speaker", String, nullable=False),
    Column("payload_json", String, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index("ix_dialogue_pr_number", "pr_number"),
)

_pr_merges = Table(
    "pr_merges",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("pr_number", Integer, nullable=False),
    Column("outcome", String, nullable=False),
    Column("base_ref", String, nullable=False),
    Column("head_ref", String, nullable=False),
    Column("merged_sha", String, nullable=True),
    Column("notes", String, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


def _engine_for(db_path: Path):
    """Build a SQLAlchemy engine pointing at *db_path* (created if needed)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"timeout": 30, "check_same_thread": False},
    )


def init_schema(db_path: Path) -> None:
    """Create the review tables if they do not exist (idempotent).

    Also self-heals existing databases by ALTER-ing columns introduced
    post-creation (SQLite has no DDL-versioning).
    """
    engine = _engine_for(db_path)
    ensure_schema_created(engine, _metadata)
    _migrate_missing_columns(engine)
    _drop_retired_columns(engine)


def _migrate_missing_columns(engine) -> None:
    """Add columns introduced post-creation to older review databases.

    Each entry is a single ``ALTER TABLE`` documented with the spec that
    introduced it.  Idempotent — checks ``has_column`` first.

    Args:
        engine: SQLAlchemy engine bound to the review database.
    """
    from sqlalchemy import inspect, text

    migrations = (("pr_reviews", "decision_pivot", "TEXT", "SP-WA-DECISION-TRACE 2c"),)

    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, column, ddl, _release in migrations:
            if not inspector.has_table(table):
                continue
            existing = {col["name"] for col in inspector.get_columns(table)}
            if column in existing:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def _drop_retired_columns(engine) -> None:
    """Drop columns retired by a later spec from older review databases.

    The mirror of :func:`_migrate_missing_columns` for removals: a column a
    spec stops writing must be dropped from pre-existing databases, since a
    ``NOT NULL`` column with no default would otherwise reject every new
    insert that omits it. SQLite 3.35+ supports ``DROP COLUMN``; the step
    is idempotent (skips when the column is already gone). Each entry names
    the spec that retired the column.

    Args:
        engine: SQLAlchemy engine bound to the review database.
    """
    from sqlalchemy import inspect, text

    drops = (("pr_merges", "verdict", "SP-RETIRE-VERDICT-ARCHIVE 10b-4"),)

    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, column, _release in drops:
            if not inspector.has_table(table):
                continue
            existing = {col["name"] for col in inspector.get_columns(table)}
            if column not in existing:
                continue
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))


def record_review(
    db_path: Path,
    *,
    pr_number: int,
    outcome: ReviewerOutcome,
) -> None:
    """Persist one reviewer's outcome to L4."""
    engine = _engine_for(db_path)
    n_blockers = sum(1 for c in outcome.comments if c.severity == "blocker")
    n_majors = sum(1 for c in outcome.comments if c.severity == "major")
    comments_json = json.dumps(
        [
            {
                "file": c.file,
                "line": c.line,
                "severity": c.severity,
                "body": c.body,
            }
            for c in outcome.comments
        ]
    )
    decision_pivot = derive_decision_pivot(outcome)
    with engine.begin() as conn:
        conn.execute(
            insert(_pr_reviews).values(
                pr_number=pr_number,
                role=outcome.role.value,
                verdict=outcome.verdict.value,
                summary=outcome.summary,
                n_comments=len(outcome.comments),
                n_blockers=n_blockers,
                n_majors=n_majors,
                model_used=outcome.model_used,
                elapsed_s=outcome.elapsed_s,
                tokens_used=outcome.tokens_used,
                comments_json=comments_json,
                created_at=datetime.now(UTC),
                decision_pivot=decision_pivot,
            )
        )


def derive_decision_pivot(outcome: ReviewerOutcome) -> str | None:
    """Return a one-line summary of what drove the reviewer's verdict.

    For REQUEST_CHANGES, the pivot is the first blocker (then first
    major if no blocker exists) — that's the comment a reader should
    open the PR for.  For APPROVE / COMMENT the pivot is ``None`` ;
    the audit row still carries ``summary`` for context, but there's
    no single comment to flag.  Consumed by the EXPLAIN path
    (SP-WA-DECISION-TRACE) so *"pourquoi PR #N a-t-elle été refusée ?"*
    has a stable answer beyond *"REQUEST_CHANGES"*.

    Args:
        outcome: The reviewer's aggregated outcome.

    Returns:
        A short string like ``"blocker @ path/file.py:42 — body"`` or
        ``None`` when no specific blocker/major drove the verdict.
    """
    if outcome.verdict.value != "REQUEST_CHANGES":
        return None
    blockers = [c for c in outcome.comments if c.severity == "blocker"]
    majors = [c for c in outcome.comments if c.severity == "major"]
    candidates = blockers or majors
    if not candidates:
        return f"verdict-only:{outcome.summary[:160].strip()}" if outcome.summary else None
    pivot = candidates[0]
    body = (pivot.body or "").strip().splitlines()[0] if pivot.body else ""
    body_short = body[:160] + ("…" if len(body) > 160 else "")
    return f"{pivot.severity} @ {pivot.file}:{pivot.line} — {body_short}"


def record_merge(
    db_path: Path,
    *,
    pr_number: int,
    outcome: str,
    base_ref: str,
    head_ref: str,
    merged_sha: str | None,
    notes: str,
) -> None:
    """Persist one auto-merge attempt to L4 ``pr_merges``.

    Args:
        db_path: SQLite path.
        pr_number: PR number.
        outcome: Short tag, see ``pr_merges.outcome`` column.
        base_ref: PR target branch.
        head_ref: PR source branch.
        merged_sha: Merge commit SHA on success, ``None`` otherwise.
        notes: Free-form context string (truncated to 1000 chars).
    """
    engine = _engine_for(db_path)
    with engine.begin() as conn:
        conn.execute(
            insert(_pr_merges).values(
                pr_number=pr_number,
                outcome=outcome,
                base_ref=base_ref,
                head_ref=head_ref,
                merged_sha=merged_sha,
                notes=notes[:1000],
                created_at=datetime.now(UTC),
            )
        )


def fetch_merged_pr_shas(db_path: Path) -> set[str]:
    """Return every recorded ``pr_merges.merged_sha`` in *db_path*.

    Consumed by the release gate's provenance check
    (SP-RELEASE-PROVENANCE-LEDGER): a commit in a ``develop -> main``
    release range is a legitimate gated-PR squash only when its own SHA
    is a member of this set.  Only rows with a non-null ``merged_sha``
    qualify -- :func:`record_merge` sets it exclusively alongside a
    real green-CI merge (``OUTCOME_MERGED``/``OUTCOME_ALREADY_MERGED``
    style outcomes); ``SKIP_*`` and ``FAILED`` attempts never carry
    one and are not provenance evidence.

    Args:
        db_path: SQLite path for the review ledger.

    Returns:
        The set of recorded merge SHAs.  Empty when the ledger has no
        such row yet, including a brand-new database.

    Raises:
        Exception: Any failure reading or initialising the ledger
            propagates unchanged -- the caller must treat a failure to
            read this ledger as an evaluation error, never as an empty
            (and therefore silently permissive) result.
    """
    init_schema(db_path)
    engine = _engine_for(db_path)
    with engine.connect() as conn:
        rows = conn.execute(
            select(_pr_merges.c.merged_sha).where(_pr_merges.c.merged_sha.is_not(None))
        ).all()
    return {row.merged_sha for row in rows if row.merged_sha}


def record_hallucination(
    db_path: Path,
    *,
    pr_number: int,
    event: GuardEvent,
) -> None:
    """Persist one :class:`GuardEvent` to L4 ``pr_hallucinations``.

    Args:
        db_path: SQLite path.
        pr_number: PR number the downgrade applies to.
        event: The :class:`GuardEvent` produced by
            :func:`apply_hallucination_guard`.
    """
    engine = _engine_for(db_path)
    with engine.begin() as conn:
        conn.execute(
            insert(_pr_hallucinations).values(
                pr_number=pr_number,
                role=event.role.value,
                file=event.file,
                line=event.line,
                original_severity=event.original_severity,
                reason=event.reason,
                tokens_found=json.dumps(list(event.tokens_found)),
                original_body=event.original_body[:2000],
                created_at=datetime.now(UTC),
            )
        )


@dataclass(frozen=True)
class DialogueEntry:
    """One dialogue turn read back from ``pr_review_dialogue``.

    Attributes:
        pr_number: GitHub PR number.
        round: ``"1"``, ``"2"``, or ``"challenge"``.
        speaker: One of ``"architect"``, ``"sentinel"``, ``"tester"``,
            ``"scribe"``, ``"coder"``.
        payload: Decoded JSON payload (reviewer outcome, challenge
            record, …) — the same shape the writer passed in.
        created_at: Insertion timestamp (UTC).
    """

    pr_number: int
    round: str
    speaker: str
    payload: dict
    created_at: datetime


def record_dialogue(
    db_path: Path,
    *,
    pr_number: int,
    round: str,
    speaker: str,
    payload: Mapping[str, Any],
) -> None:
    """Persist one dialogue turn to ``pr_review_dialogue``.

    Args:
        db_path: SQLite path.  The table is created on first call via
            :func:`init_schema`-style auto-migration.
        pr_number: GitHub PR number.
        round: ``"1"`` (initial reviewer outcome), ``"2"`` (round-2
            self-revision), or ``"challenge"`` (Coder challenge record).
        speaker: ``"architect"``, ``"sentinel"``, ``"tester"``,
            ``"scribe"``, or ``"coder"``.
        payload: JSON-serialisable mapping carrying the verdict,
            comments, or challenge record body.
    """
    init_schema(db_path)
    engine = _engine_for(db_path)
    with engine.begin() as conn:
        conn.execute(
            insert(_pr_review_dialogue).values(
                pr_number=pr_number,
                round=round,
                speaker=speaker,
                payload_json=json.dumps(dict(payload), default=str)[:32000],
                created_at=datetime.now(UTC),
            )
        )


def fetch_dialogue(
    pr_number: int,
    *,
    db_path: Path,
) -> list[DialogueEntry]:
    """Return every dialogue turn for ``pr_number`` in chronological order.

    Args:
        pr_number: GitHub PR number.
        db_path: SQLite path.

    Returns:
        Ordered list of :class:`DialogueEntry` (oldest first).  An
        empty list when the table does not exist yet or no turn was
        recorded for this PR.
    """
    engine = _engine_for(db_path)
    ensure_schema_created(engine, _metadata)
    stmt = (
        select(_pr_review_dialogue)
        .where(_pr_review_dialogue.c.pr_number == pr_number)
        .order_by(_pr_review_dialogue.c.id)
    )
    out: list[DialogueEntry] = []
    with engine.connect() as conn:
        for row in conn.execute(stmt):
            try:
                payload = json.loads(row.payload_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                _log.warning(
                    "persistence.dialogue_payload_unparseable",
                    pr_number=pr_number,
                    row_id=row.id,
                )
                payload = {}
            if not isinstance(payload, dict):
                payload = {"value": payload}
            out.append(
                DialogueEntry(
                    pr_number=int(row.pr_number),
                    round=str(row.round),
                    speaker=str(row.speaker),
                    payload=payload,
                    created_at=row.created_at,
                )
            )
    return out


def record_coder_response(
    db_path: Path,
    *,
    pr_number: int,
    plan: dict,
    model_used: str,
    elapsed_s: float,
    tokens_used: int,
) -> None:
    """Persist the Coder's fix-plan to L4."""
    engine = _engine_for(db_path)
    with engine.begin() as conn:
        conn.execute(
            insert(_pr_coder_responses).values(
                pr_number=pr_number,
                n_fixes_planned=len(plan.get("fixes", []) or []),
                commit_message=str(plan.get("commit_message", ""))[:1000],
                summary=str(plan.get("summary", ""))[:240],
                model_used=model_used,
                elapsed_s=elapsed_s,
                tokens_used=tokens_used,
                fixes_json=json.dumps(plan.get("fixes", []) or [])[:32000],
                created_at=datetime.now(UTC),
            )
        )
