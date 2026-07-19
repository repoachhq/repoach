"""SQLite persistence for per-cell health probes (SP-CHAINPILOT-PROBE-SWEEP).

Phase 2a-2 of the Chain Autopilot arc. Each probe-matrix sweep appends one row
per ``(provider, model)`` cell to the ``cell_health_probe`` table, building a
cross-session time-series of how every cell behaves (status, latency, and how
much it reasoned vs answered) — the standing record the decision engine reads.

It mirrors the shape of :mod:`repoach.health.store` (``nim_health_probe``)
but lives in the chainpilot domain as its own leaf and owns its own table, so
the arc stays self-contained rather than reaching into the review-health store.
The small duplication of the SQLite-engine boilerplate is the deliberate cost
of that decoupling.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

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

from repoach.core.logging import get_logger
from repoach.llm_proxy.providers.cell_probe import CellHealth

_log = get_logger(__name__)

_metadata = MetaData()

_cell_health_probe = Table(
    "cell_health_probe",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    Column("provider_id", String, nullable=False),
    Column("model_id", String, nullable=False),
    Column("status", String, nullable=False),
    Column("latency_s", Float, nullable=True),
    Column("content_chars", Integer, nullable=False),
    Column("reasoning_chars", Integer, nullable=False),
    Column("detail", String, nullable=False),
    Index("ix_cell_health_recorded_at", "recorded_at"),
)

__all__ = [
    "CellProbeRow",
    "fetch_cell_probes",
    "init_cell_health_schema",
    "record_cell_probes",
]


def _engine_for(db_path: Path):
    """Build a SQLAlchemy engine pointing at *db_path* (created if needed)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"timeout": 30, "check_same_thread": False},
    )


def init_cell_health_schema(db_path: Path) -> None:
    """Create the ``cell_health_probe`` table if it does not exist (idempotent)."""
    engine = _engine_for(db_path)
    _metadata.create_all(engine, checkfirst=True)


@dataclass(frozen=True)
class CellProbeRow:
    """One ``cell_health_probe`` row read back from the store.

    Attributes:
        recorded_at: Sweep timestamp (UTC); shared by every row of one sweep.
        provider_id: The provider the cell was probed on.
        model_id: The probed model id.
        status: ``ok`` / ``slow`` / ``empty`` / ``error``.
        latency_s: Probe wall-clock seconds, or ``None``.
        content_chars: Length of the visible assistant text.
        reasoning_chars: Length of the hidden reasoning text.
        detail: Short note (content preview, error class, or skip reason).
    """

    recorded_at: datetime
    provider_id: str
    model_id: str
    status: str
    latency_s: float | None
    content_chars: int
    reasoning_chars: int
    detail: str


def record_cell_probes(
    db_path: Path,
    probes: Sequence[CellHealth],
    *,
    recorded_at: datetime,
) -> int:
    """Persist one sweep; every row shares *recorded_at*.

    Args:
        db_path: SQLite path (the table is created on first call).
        probes: The :class:`CellHealth` results of one sweep.
        recorded_at: One UTC timestamp stamped on the whole sweep, so a sweep's
            rows group cleanly in later analysis. Passed in (not read from the
            wall clock here) to keep the store deterministic under test.

    Returns:
        The number of rows written.
    """
    init_cell_health_schema(db_path)
    if not probes:
        return 0
    rows = [
        {
            "recorded_at": recorded_at,
            "provider_id": probe.provider_id,
            "model_id": probe.model_id,
            "status": probe.status,
            "latency_s": probe.latency_s,
            "content_chars": probe.content_chars,
            "reasoning_chars": probe.reasoning_chars,
            "detail": probe.detail,
        }
        for probe in probes
    ]
    engine = _engine_for(db_path)
    with engine.begin() as conn:
        conn.execute(insert(_cell_health_probe), rows)
    return len(rows)


def fetch_cell_probes(
    db_path: Path,
    *,
    since: datetime | None = None,
    provider_id: str | None = None,
    model_id: str | None = None,
    limit: int | None = None,
) -> list[CellProbeRow]:
    """Return cell-probe rows newest-first, optionally filtered.

    Args:
        db_path: SQLite path.
        since: When set, only rows with ``recorded_at >= since``.
        provider_id: When set, only rows for that provider.
        model_id: When set, only rows for that model.
        limit: When set, cap the number of rows returned.

    Returns:
        Matching :class:`CellProbeRow` records, newest first.
    """
    engine = _engine_for(db_path)
    _metadata.create_all(engine, checkfirst=True)
    stmt = select(_cell_health_probe).order_by(_cell_health_probe.c.id.desc())
    if since is not None:
        stmt = stmt.where(_cell_health_probe.c.recorded_at >= since)
    if provider_id is not None:
        stmt = stmt.where(_cell_health_probe.c.provider_id == provider_id)
    if model_id is not None:
        stmt = stmt.where(_cell_health_probe.c.model_id == model_id)
    if limit is not None:
        stmt = stmt.limit(limit)
    out: list[CellProbeRow] = []
    with engine.connect() as conn:
        for row in conn.execute(stmt):
            recorded_at = row.recorded_at
            if recorded_at is not None and recorded_at.tzinfo is None:
                recorded_at = recorded_at.replace(tzinfo=UTC)
            out.append(
                CellProbeRow(
                    recorded_at=recorded_at,
                    provider_id=str(row.provider_id),
                    model_id=str(row.model_id),
                    status=str(row.status),
                    latency_s=row.latency_s,
                    content_chars=int(row.content_chars),
                    reasoning_chars=int(row.reasoning_chars),
                    detail=str(row.detail),
                )
            )
    return out
