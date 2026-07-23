"""Write-through persistence and boot rehydration for the health breaker.

`SP-PROXY-STATE-PERSIST`. :class:`~repoach.llm_proxy.routing.breaker.
BreakerState` is deliberately clock-free (its own module docstring commits
to this) and process-memory-only — every trip, consecutive-failure count,
and slow-history window vanishes the instant the proxy process exits. This
leaf adapter gives it a durable, write-through memory: every mutation that
leaves a ref DOWN is mirrored to a ``breaker_trip_state`` row in the shared
SQLite file (``settings.breaker_probe_seed_db``) with a wall-clock expiry,
and at boot :func:`rehydrate_breaker_from_state` restores every row whose
TTL has not lapsed — computed from ``datetime.now(UTC)`` since the
pre-restart ``time.monotonic()`` origin does not survive a process
restart.

Kept independent of :mod:`repoach.health.store` (no import from it, though
the ``Table`` + ``create_engine(..., checkfirst=True)`` shape mirrors it):
the new table is unrelated data living in the same shared file, so no
dependency on ``SP-HEALTH-STORE-NEUTRALIZE`` is introduced.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loguru import logger
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    delete,
    select,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from repoach.llm_proxy.routing.breaker import BreakerState
from repoach.llm_proxy.routing.refs import ModelRef

_metadata = MetaData()

_breaker_trip_state = Table(
    "breaker_trip_state",
    _metadata,
    Column("ref", String, primary_key=True),
    Column("provider_id", String, nullable=False),
    Column("model", String, nullable=False),
    Column("down_until_utc", DateTime(timezone=True), nullable=False),
    Column("reason", String, nullable=False),
    Column("consecutive_failures", Integer, nullable=False),
    Column("slow_history_json", String, nullable=False),
    Column("ttl_s", Float, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


def _engine_for(db_path: Path):
    """Build a SQLAlchemy engine pointing at *db_path* (created if needed)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"timeout": 30, "check_same_thread": False},
    )


def init_breaker_state_schema(db_path: Path) -> None:
    """Create the ``breaker_trip_state`` table if it does not exist (idempotent)."""
    engine = _engine_for(db_path)
    _metadata.create_all(engine, checkfirst=True)


def _as_aware_utc(value: datetime) -> datetime:
    """Attach the UTC tzinfo to a naive ``datetime`` read back from SQLite."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def persist_state(
    breaker: BreakerState,
    ref: ModelRef,
    *,
    db_path: Path,
    monotonic_now: float,
    wall_clock_now: datetime,
) -> None:
    """Write-through *ref*'s current breaker state as one upserted row.

    When ``ref`` is currently down (its ``_down_until`` entry is set and
    still in the future relative to ``monotonic_now``), upserts one row
    projecting the remaining monotonic window onto ``wall_clock_now`` so
    the expiry survives a process restart. When ``ref`` is NOT currently
    down (recovered, or its TTL already lapsed), deletes its row instead.

    Never raises — this sits on the hot dispatch path and a persistence
    hiccup must never affect live failover; any DB error is logged and
    swallowed.

    Args:
        breaker: The singleton whose state for ``ref`` is being mirrored.
        ref: The provider/model reference to write through.
        db_path: The shared SQLite path.
        monotonic_now: A ``time.monotonic()`` reading.
        wall_clock_now: A ``datetime.now(UTC)`` reading taken at the same
            instant as ``monotonic_now``.
    """
    try:
        engine = _engine_for(db_path)
        _metadata.create_all(engine, checkfirst=True)
        down_until = breaker._down_until.get(ref)
        if down_until is None or down_until <= monotonic_now:
            with engine.begin() as conn:
                conn.execute(
                    delete(_breaker_trip_state).where(_breaker_trip_state.c.ref == str(ref))
                )
            return

        ttl_s = down_until - monotonic_now
        row = {
            "ref": str(ref),
            "provider_id": ref.provider_id,
            "model": ref.model,
            "down_until_utc": wall_clock_now + timedelta(seconds=ttl_s),
            "reason": breaker._down_reason.get(ref, ""),
            "consecutive_failures": breaker._consecutive_failures.get(ref, 0),
            "slow_history_json": json.dumps(breaker._slow_history.get(ref, [])),
            "ttl_s": ttl_s,
            "updated_at": wall_clock_now,
        }
        with engine.begin() as conn:
            upsert = sqlite_insert(_breaker_trip_state).values(**row)
            upsert = upsert.on_conflict_do_update(index_elements=["ref"], set_=row)
            conn.execute(upsert)
    except Exception as exc:
        logger.warning(
            "breaker_state_persist_failed",
            ref=str(ref),
            error_type=type(exc).__name__,
            error=str(exc),
        )


def rehydrate_breaker_from_state(
    breaker: BreakerState,
    *,
    db_path: Path,
    monotonic_now: float,
    wall_clock_now: datetime,
) -> int:
    """Restore every still-live persisted row into *breaker*.

    For each ``breaker_trip_state`` row, computes ``remaining_s`` from the
    wall-clock delta. A row whose TTL has already lapsed (``remaining_s
    <= 0``) is pruned and skipped, and so is a row whose
    ``slow_history_json`` fails to parse (a malformed row, logged). Every
    other row is restored via :meth:`BreakerState.restore` with its
    remaining TTL clamped to ``ttl_s`` (the value in force at persist
    time), bounding the damage of a backward system-clock step between
    shutdown and boot.

    Raises on a genuine DB failure — mirrors
    :func:`repoach.llm_proxy.routing.probe_seed.seed_breaker_from_probes`,
    which also does not swallow; the one caller,
    :meth:`repoach.llm_proxy.api.runtime.AppRuntime.
    _seed_breaker_from_persisted_state`, wraps this in its own
    log-and-swallow ``try/except`` so the proxy always finishes booting.

    Args:
        breaker: The singleton to restore rows into.
        db_path: The shared SQLite path.
        monotonic_now: A ``time.monotonic()`` reading taken at boot.
        wall_clock_now: A ``datetime.now(UTC)`` reading taken at the same
            instant as ``monotonic_now``.

    Returns:
        The number of rows restored.
    """
    engine = _engine_for(db_path)
    _metadata.create_all(engine, checkfirst=True)
    restored = 0
    stale_refs: list[str] = []
    with engine.begin() as conn:
        rows = conn.execute(select(_breaker_trip_state)).fetchall()
        for row in rows:
            down_until_utc = _as_aware_utc(row.down_until_utc)
            remaining_s = (down_until_utc - wall_clock_now).total_seconds()
            if remaining_s <= 0:
                stale_refs.append(row.ref)
                continue
            try:
                slow_history = json.loads(row.slow_history_json)
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning(
                    "breaker_state_row_corrupt",
                    ref=row.ref,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                stale_refs.append(row.ref)
                continue
            effective_ttl_s = min(remaining_s, row.ttl_s)
            breaker.restore(
                ModelRef(provider_id=row.provider_id, model=row.model),
                now=monotonic_now,
                ttl_s=effective_ttl_s,
                reason=row.reason,
                consecutive_failures=row.consecutive_failures,
                slow_history=slow_history,
            )
            restored += 1
        if stale_refs:
            conn.execute(
                delete(_breaker_trip_state).where(_breaker_trip_state.c.ref.in_(stale_refs))
            )
    return restored
