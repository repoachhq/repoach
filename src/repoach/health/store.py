"""SQLite persistence for NIM chain head-health probes (neutral leaf).

`SP-HEALTH-STORE-NEUTRALIZE` (moved from ``review.chain_health_store``).
Each ``ferova monitor-chains`` sweep appends one row per tier to the
``nim_health_probe`` table in the shared review DB, building a
cross-session time-series of NIM head behaviour (status, latency) for
later analysis. It imports only ``sqlalchemy``, ``core.logging`` and the
neutral :mod:`repoach.health.model_health`, so the llm_proxy can read
probe history to seed its breaker without an import cycle.
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
from repoach.health.model_health import ModelHealth

_log = get_logger(__name__)

_metadata = MetaData()

_nim_health_probe = Table(
    "nim_health_probe",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    Column("tier", String, nullable=False),
    Column("model", String, nullable=False),
    Column("status", String, nullable=False),
    Column("latency_s", Float, nullable=True),
    Column("content_chars", Integer, nullable=False),
    Column("detail", String, nullable=False),
    Index("ix_nim_health_recorded_at", "recorded_at"),
)


def _engine_for(db_path: Path):
    """Build a SQLAlchemy engine pointing at *db_path* (created if needed)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"timeout": 30, "check_same_thread": False},
    )


def init_nim_health_schema(db_path: Path) -> None:
    """Create the ``nim_health_probe`` table if it does not exist (idempotent)."""
    engine = _engine_for(db_path)
    _metadata.create_all(engine, checkfirst=True)


@dataclass(frozen=True)
class ProbeRow:
    """One ``nim_health_probe`` row read back from the store.

    Attributes:
        recorded_at: Sweep timestamp (UTC); shared by every row of one run.
        tier: Capability tier probed.
        model: The probed model id, or ``provider/model`` for a skip.
        status: ``ok`` / ``slow`` / ``empty`` / ``error`` / ``skipped``.
        latency_s: Probe wall-clock seconds, or ``None``.
        content_chars: Length of the returned assistant text.
        detail: Short note (content preview, error class, or skip reason).
    """

    recorded_at: datetime
    tier: str
    model: str
    status: str
    latency_s: float | None
    content_chars: int
    detail: str


def record_probes(
    db_path: Path,
    probes: Sequence[ModelHealth],
    *,
    recorded_at: datetime,
) -> int:
    """Persist one sweep; every row shares *recorded_at*.

    Args:
        db_path: SQLite path (the table is created on first call).
        probes: The :class:`ModelHealth` results of one sweep.
        recorded_at: One UTC timestamp stamped on the whole sweep, so a
            run's rows group cleanly in later analysis. Passed in (not
            read from the wall clock here) to keep the store
            deterministic under test.

    Returns:
        The number of rows written.
    """
    init_nim_health_schema(db_path)
    if not probes:
        return 0
    rows = [
        {
            "recorded_at": recorded_at,
            "tier": probe.tier,
            "model": probe.model,
            "status": probe.status,
            "latency_s": probe.latency_s,
            "content_chars": probe.content_chars,
            "detail": probe.detail,
        }
        for probe in probes
    ]
    engine = _engine_for(db_path)
    with engine.begin() as conn:
        conn.execute(insert(_nim_health_probe), rows)
    return len(rows)


def fetch_probes(
    db_path: Path,
    *,
    since: datetime | None = None,
    tier: str | None = None,
    limit: int | None = None,
) -> list[ProbeRow]:
    """Return probe rows newest-first, optionally filtered.

    Args:
        db_path: SQLite path.
        since: When set, only rows with ``recorded_at >= since``.
        tier: When set, only rows for that tier.
        limit: When set, cap the number of rows returned.

    Returns:
        Matching :class:`ProbeRow` records, newest first.
    """
    engine = _engine_for(db_path)
    _metadata.create_all(engine, checkfirst=True)
    stmt = select(_nim_health_probe).order_by(_nim_health_probe.c.id.desc())
    if since is not None:
        stmt = stmt.where(_nim_health_probe.c.recorded_at >= since)
    if tier is not None:
        stmt = stmt.where(_nim_health_probe.c.tier == tier)
    if limit is not None:
        stmt = stmt.limit(limit)
    out: list[ProbeRow] = []
    with engine.connect() as conn:
        for row in conn.execute(stmt):
            recorded_at = row.recorded_at
            if recorded_at is not None and recorded_at.tzinfo is None:
                recorded_at = recorded_at.replace(tzinfo=UTC)
            out.append(
                ProbeRow(
                    recorded_at=recorded_at,
                    tier=str(row.tier),
                    model=str(row.model),
                    status=str(row.status),
                    latency_s=row.latency_s,
                    content_chars=int(row.content_chars),
                    detail=str(row.detail),
                )
            )
    return out
