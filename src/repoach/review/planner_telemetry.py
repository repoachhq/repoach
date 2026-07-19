"""Per-attempt planner telemetry — one row per rejected plan attempt.

SP-PLAN-QUALITY step 4/6: every rejected Planner attempt persists
``(spec_id, attempt, violated_rule)`` so oscillation across the
parse-attempt budget is measurable. The table lives in the same
SQLite database as the rest of the review ledger (the
``get_settings().db_path`` convention) and self-heals like the other
schema additions.

The module follows the imperative SQLAlchemy Core scaffold of
:mod:`findings` and :mod:`persistence` exactly: module-level
``MetaData()`` + ``Table``, ``_engine_for(db_path)`` that mkdirs the
parent, ``init_planner_telemetry_schema`` doing
``metadata.create_all(engine, checkfirst=True)`` plus an (initially
empty) ``_migrate_missing_columns``-style scaffold. All functions
take ``db_path: Path`` as explicit first parameter — the module
never calls ``get_settings()`` itself (the persistence-module
convention; the call site resolves the path).

Failure policy: ``record_planner_attempt`` wraps its whole
engine/insert path in ``try/except`` that LOGS a warning
(``planner_telemetry.record_failed`` — the no-silent-except gate
requires the log) and returns ``False`` instead of raising. A
telemetry failure must NEVER break a planning session.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    insert,
    select,
)
from sqlalchemy.engine import Engine

from ..core.logging import get_logger

logger = get_logger(__name__)

_metadata = MetaData()

planner_attempts = Table(
    "planner_attempts",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("spec_id", String, nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("violated_rule", String, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
)


def _engine_for(db_path: Path) -> Engine:
    """Return a SQLite engine, creating the parent directory if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{db_path}")


def init_planner_telemetry_schema(db_path: Path) -> None:
    """Create the planner_attempts table if absent (idempotent).

    Also self-heals existing databases by ALTER-ing columns introduced
    post-creation (SQLite has no DDL-versioning). The migration list
    is intentionally empty at introduction time; future specs append
    entries here.
    """
    engine = _engine_for(db_path)
    _metadata.create_all(engine, checkfirst=True)
    _migrate_missing_columns(engine)


def _migrate_missing_columns(engine: Engine) -> None:
    """Add columns introduced post-creation to older telemetry databases.

    Each entry is a single ``ALTER TABLE`` documented with the spec that
    introduced it. Idempotent — checks ``has_column`` first.

    Args:
        engine: SQLAlchemy engine bound to the telemetry database.
    """
    from sqlalchemy import inspect, text

    migrations: tuple[tuple[str, str, str, str], ...] = ()

    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, column, ddl, _release in migrations:
            if not inspector.has_table(table):
                continue
            existing = {col["name"] for col in inspector.get_columns(table)}
            if column in existing:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def record_planner_attempt(
    db_path: Path,
    *,
    spec_id: str,
    attempt: int,
    violated_rule: str,
) -> bool:
    """Persist one rejected Planner attempt. Never raises.

    The whole engine/insert path is wrapped in ``try/except`` so a
    telemetry failure (uncreatable parent directory, locked database,
    schema drift) cannot break a planning session. On failure the
    function logs a ``planner_telemetry.record_failed`` warning and
    returns ``False``; on success it returns ``True``.

    Args:
        db_path: The review ledger database.
        spec_id: Spec the rejected attempt planned for.
        attempt: 1-based attempt index within the session.
        violated_rule: First 300 chars of the validation or strict-layer
            reason that rejected the attempt.

    Returns:
        ``True`` when the row was inserted; ``False`` on any failure.
    """
    try:
        init_planner_telemetry_schema(db_path)
        engine = _engine_for(db_path)
        with engine.begin() as conn:
            conn.execute(
                insert(planner_attempts).values(
                    spec_id=spec_id,
                    attempt=attempt,
                    violated_rule=violated_rule[:300],
                    recorded_at=datetime.now(UTC),
                )
            )
        return True
    except Exception as exc:
        logger.warning(
            "planner_telemetry.record_failed",
            spec_id=spec_id,
            attempt=attempt,
            error=str(exc)[:200],
        )
        return False


def fetch_planner_attempts(db_path: Path, spec_id: str | None = None) -> list[dict[str, object]]:
    """Return recorded planner attempts, ordered by id.

    Args:
        db_path: The review ledger database.
        spec_id: Optional spec filter; ``None`` returns every row.

    Returns:
        One dict per row with ``spec_id`` / ``attempt`` /
        ``violated_rule`` / ``recorded_at`` keys, oldest first.
    """
    init_planner_telemetry_schema(db_path)
    engine = _engine_for(db_path)
    stmt = select(planner_attempts).order_by(planner_attempts.c.id)
    if spec_id is not None:
        stmt = stmt.where(planner_attempts.c.spec_id == spec_id)
    with engine.connect() as conn:
        rows = list(conn.execute(stmt).mappings())
    return [
        {
            "spec_id": row["spec_id"],
            "attempt": int(row["attempt"]),
            "violated_rule": row["violated_rule"],
            "recorded_at": row["recorded_at"],
        }
        for row in rows
    ]
