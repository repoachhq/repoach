"""Chain mutation journal + changelog (SP-CHAINPILOT-AUDIT-LOG).

Phase 3c of the Chain Autopilot arc — the documented half of Principle 7
(automatic but documented). It persists every chain mutation the engine emits
with what changed, the signal that triggered it, when, and free-text context,
so a mutation is never silent and its rationale is never lost.

It carries an ``applied`` flag: a shadow / dry-run run (3d proposing without
writing ``chains.env``) journals with ``applied=False``, a real apply with
``applied=True`` — the same ledger records both, which is how confidence is
built before the loop touches the live file. The module mirrors the arc's
sibling persistence leaves (``cell_probe_store`` / ``effort_probe_store``):
idempotent init, a batch record with an injected ``recorded_at``, a filtered
fetch; :func:`render_changelog` is a pure human-readable rendering.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    insert,
    select,
)

from ferova.review.decision import PlannedMutation

_metadata = MetaData()

_chain_mutation_log = Table(
    "chain_mutation_log",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    Column("kind", String, nullable=False),
    Column("model", String, nullable=False),
    Column("provider", String, nullable=True),
    Column("metric", String, nullable=True),
    Column("reason", String, nullable=False),
    Column("applied", Boolean, nullable=False),
    Column("detail", String, nullable=False),
    Index("ix_chain_mutation_recorded_at", "recorded_at"),
)

__all__ = [
    "MutationRecord",
    "fetch_mutations",
    "init_audit_schema",
    "record_mutations",
    "render_changelog",
]


def _engine_for(db_path: Path):
    """Build a SQLAlchemy engine pointing at *db_path* (created if needed)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"timeout": 30, "check_same_thread": False},
    )


def init_audit_schema(db_path: Path) -> None:
    """Create the ``chain_mutation_log`` table if it does not exist (idempotent)."""
    engine = _engine_for(db_path)
    _metadata.create_all(engine, checkfirst=True)


@dataclass(frozen=True)
class MutationRecord:
    """One ``chain_mutation_log`` row read back from the journal.

    Attributes:
        recorded_at: When the mutation was journaled (UTC); shared by a run.
        kind: The mutation kind (``MutationKind`` value).
        model: The model the mutation targeted.
        provider: The dropped provider (``drop_provider`` only), else ``None``.
        metric: The metric that drove a ``demote`` / ``promote``, else ``None``.
        reason: The triggering signal / justification.
        applied: ``True`` when written to ``chains.env``, ``False`` for a shadow
            (dry-run) journal entry.
        detail: Free-text context the caller supplied (e.g. what it replaced).
    """

    recorded_at: datetime
    kind: str
    model: str
    provider: str | None
    metric: str | None
    reason: str
    applied: bool
    detail: str


def record_mutations(
    db_path: Path,
    mutations: Sequence[PlannedMutation],
    *,
    applied: bool,
    recorded_at: datetime,
    detail: str = "",
) -> int:
    """Persist a batch of planned mutations to the journal.

    Args:
        db_path: SQLite path (the table is created on first call).
        mutations: The mutations to journal; an empty batch records nothing.
        applied: Whether these were actually written to ``chains.env``
            (``True``) or merely proposed in a shadow run (``False``).
        recorded_at: One UTC timestamp stamped on every row of this batch.
        detail: Optional free-text context applied to every row.

    Returns:
        The number of rows written.
    """
    init_audit_schema(db_path)
    if not mutations:
        return 0
    rows = [
        {
            "recorded_at": recorded_at,
            "kind": str(mutation.kind.value),
            "model": mutation.model,
            "provider": mutation.provider,
            "metric": mutation.metric,
            "reason": mutation.reason,
            "applied": applied,
            "detail": detail,
        }
        for mutation in mutations
    ]
    engine = _engine_for(db_path)
    with engine.begin() as conn:
        conn.execute(insert(_chain_mutation_log), rows)
    return len(rows)


def fetch_mutations(
    db_path: Path,
    *,
    since: datetime | None = None,
    limit: int | None = None,
) -> list[MutationRecord]:
    """Return journaled mutations newest-first, optionally filtered.

    Args:
        db_path: SQLite path.
        since: When set, only rows with ``recorded_at >= since``.
        limit: When set, cap the number of rows returned.

    Returns:
        Matching :class:`MutationRecord`s, newest first.
    """
    init_audit_schema(db_path)
    stmt = select(_chain_mutation_log).order_by(_chain_mutation_log.c.id.desc())
    if since is not None:
        stmt = stmt.where(_chain_mutation_log.c.recorded_at >= since)
    if limit is not None:
        stmt = stmt.limit(limit)
    out: list[MutationRecord] = []
    engine = _engine_for(db_path)
    with engine.connect() as conn:
        for row in conn.execute(stmt):
            recorded_at = row.recorded_at
            if recorded_at is not None and recorded_at.tzinfo is None:
                recorded_at = recorded_at.replace(tzinfo=UTC)
            out.append(
                MutationRecord(
                    recorded_at=recorded_at,
                    kind=str(row.kind),
                    model=str(row.model),
                    provider=row.provider,
                    metric=row.metric,
                    reason=str(row.reason),
                    applied=bool(row.applied),
                    detail=str(row.detail),
                )
            )
    return out


def render_changelog(records: Sequence[MutationRecord]) -> str:
    """Render journal records as a human-readable changelog.

    Args:
        records: The records to render (typically a :func:`fetch_mutations`
            result).

    Returns:
        One line per record — timestamp, APPLIED/SHADOW, kind, target, and
        reason — joined by newlines; the empty string for no records.
    """
    lines: list[str] = []
    for record in records:
        target = record.model
        if record.provider is not None:
            target = f"{target}@{record.provider}"
        if record.metric is not None:
            target = f"{target} [{record.metric}]"
        tag = "APPLIED" if record.applied else "SHADOW"
        lines.append(
            f"{record.recorded_at.isoformat()} {tag} {record.kind} {target} — {record.reason}"
        )
    return "\n".join(lines)
