"""SQLite persistence for reasoned-at-effort probes (SP-CHAINPILOT-EFFORT-SWEEP).

Phase 2a-3-ii. Each effort sweep (:mod:`effort_sweep`) appends one row per
``(provider, model)`` cell to the ``cell_effort_probe`` table — the
reasoned-at-effort counterpart of 2a-2's baseline ``cell_health_probe``. It
carries the same per-cell observation plus the Phase 2 policy-B attribution:
``effort_used`` (the effort requested, nullable) and ``model_used`` (the model
probed). This is the standing record the effort resolver (2a-3-iii) reads.

It mirrors :mod:`ferova.llm_proxy.providers.cell_probe_store` but owns its
own table, keeping baseline-health and reasoned-at-effort as two disjoint
series. The small duplication of the SQLite-engine boilerplate is the deliberate
cost of that decoupling, as 2a-2 chose for its store.
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

from ferova.core.logging import get_logger
from ferova.llm_proxy.providers.effort_sweep import EffortProbe

_log = get_logger(__name__)

_metadata = MetaData()

_cell_effort_probe = Table(
    "cell_effort_probe",
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
    Column("effort_used", String, nullable=True),
    Column("model_used", String, nullable=False),
    Index("ix_cell_effort_recorded_at", "recorded_at"),
)

__all__ = [
    "EffortProbeRow",
    "fetch_effort_probes",
    "init_cell_effort_schema",
    "record_effort_probes",
]


def _engine_for(db_path: Path):
    """Build a SQLAlchemy engine pointing at *db_path* (created if needed)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"timeout": 30, "check_same_thread": False},
    )


def init_cell_effort_schema(db_path: Path) -> None:
    """Create the ``cell_effort_probe`` table if it does not exist (idempotent)."""
    engine = _engine_for(db_path)
    _metadata.create_all(engine, checkfirst=True)


@dataclass(frozen=True)
class EffortProbeRow:
    """One ``cell_effort_probe`` row read back from the store.

    Attributes:
        recorded_at: Sweep timestamp (UTC); shared by every row of one sweep.
        provider_id: The provider the cell was probed on.
        model_id: The probed model id.
        status: ``ok`` / ``slow`` / ``empty`` / ``error``.
        latency_s: Probe wall-clock seconds, or ``None``.
        content_chars: Length of the visible assistant text.
        reasoning_chars: Length of the hidden reasoning text.
        detail: Short note (content preview, error class, or skip reason).
        effort_used: The reasoning effort requested for this probe, or ``None``
            for a provider with no effort knob.
        model_used: The model actually probed (mirrors ``model_id``).
    """

    recorded_at: datetime
    provider_id: str
    model_id: str
    status: str
    latency_s: float | None
    content_chars: int
    reasoning_chars: int
    detail: str
    effort_used: str | None
    model_used: str


def record_effort_probes(
    db_path: Path,
    probes: Sequence[EffortProbe],
    *,
    recorded_at: datetime,
) -> int:
    """Persist one effort sweep; every row shares *recorded_at*.

    Args:
        db_path: SQLite path (the table is created on first call).
        probes: The :class:`EffortProbe` results of one effort sweep.
        recorded_at: One UTC timestamp stamped on the whole sweep, so a sweep's
            rows group cleanly in later analysis. Passed in (not read from the
            wall clock here) to keep the store deterministic under test.

    Returns:
        The number of rows written.
    """
    init_cell_effort_schema(db_path)
    if not probes:
        return 0
    rows = [
        {
            "recorded_at": recorded_at,
            "provider_id": probe.health.provider_id,
            "model_id": probe.health.model_id,
            "status": probe.health.status,
            "latency_s": probe.health.latency_s,
            "content_chars": probe.health.content_chars,
            "reasoning_chars": probe.health.reasoning_chars,
            "detail": probe.health.detail,
            "effort_used": probe.effort_used,
            "model_used": probe.model_used,
        }
        for probe in probes
    ]
    engine = _engine_for(db_path)
    with engine.begin() as conn:
        conn.execute(insert(_cell_effort_probe), rows)
    return len(rows)


def fetch_effort_probes(
    db_path: Path,
    *,
    since: datetime | None = None,
    provider_id: str | None = None,
    model_id: str | None = None,
    limit: int | None = None,
) -> list[EffortProbeRow]:
    """Return effort-probe rows newest-first, optionally filtered.

    Args:
        db_path: SQLite path.
        since: When set, only rows with ``recorded_at >= since``.
        provider_id: When set, only rows for that provider.
        model_id: When set, only rows for that model.
        limit: When set, cap the number of rows returned.

    Returns:
        Matching :class:`EffortProbeRow` records, newest first.
    """
    engine = _engine_for(db_path)
    _metadata.create_all(engine, checkfirst=True)
    stmt = select(_cell_effort_probe).order_by(_cell_effort_probe.c.id.desc())
    if since is not None:
        stmt = stmt.where(_cell_effort_probe.c.recorded_at >= since)
    if provider_id is not None:
        stmt = stmt.where(_cell_effort_probe.c.provider_id == provider_id)
    if model_id is not None:
        stmt = stmt.where(_cell_effort_probe.c.model_id == model_id)
    if limit is not None:
        stmt = stmt.limit(limit)
    out: list[EffortProbeRow] = []
    with engine.connect() as conn:
        for row in conn.execute(stmt):
            recorded_at = row.recorded_at
            if recorded_at is not None and recorded_at.tzinfo is None:
                recorded_at = recorded_at.replace(tzinfo=UTC)
            out.append(
                EffortProbeRow(
                    recorded_at=recorded_at,
                    provider_id=str(row.provider_id),
                    model_id=str(row.model_id),
                    status=str(row.status),
                    latency_s=row.latency_s,
                    content_chars=int(row.content_chars),
                    reasoning_chars=int(row.reasoning_chars),
                    detail=str(row.detail),
                    effort_used=None if row.effort_used is None else str(row.effort_used),
                    model_used=str(row.model_used),
                )
            )
    return out
