"""The ``generated_checks`` table: one row per check run of a declared generated artefact.

The review job records the runs the verification job's evidence names at
the reviewed head (SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ G2); a later
head's re-check settles findings from them (G3, G6) through
:func:`settlements_at_head`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    insert,
    select,
)
from sqlalchemy.engine import Engine

from ..core.sqlite_schema_init import ensure_schema_created
from .generated_artefacts import (
    GENERATED_FINDER,
    GeneratedCheck,
    GeneratedState,
    GeneratedTiming,
    ReverifyOutcome,
    by_path,
    check_cause,
    path_verified,
)

_metadata = MetaData()

generated_checks = Table(
    "generated_checks",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("pr_number", Integer, nullable=False, index=True),
    Column("head_sha", String, nullable=False),
    Column("path", String, nullable=False),
    Column("argv", Text, nullable=False),
    Column("exit_code", Integer, nullable=True),
    Column("duration_ms", Integer, nullable=False),
    Column("output_tail", Text, nullable=False),
    Column("state", String, nullable=False),
    Column("script_ref", String, nullable=False),
    Column("runs_for_path", Integer, nullable=False),
    Column("sources_changed", Text, nullable=False),
    Column("producer_inputs_changed", Text, nullable=False),
    Column("artefact_sha256", String, nullable=False),
    Column("checks_ms", Integer, nullable=True),
    Column("overhead_ms", Integer, nullable=True),
    Column("recorded_at", String, nullable=False),
)
"""One row per check run at a head (``db:table:generated_checks``)."""


def _engine_for(db_path: Path) -> Engine:
    """Return a SQLite engine on ``db_path``, creating its directory."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{db_path}")


def init_generated_schema(db_path: Path) -> None:
    """Create the ``generated_checks`` table if absent (idempotent)."""
    ensure_schema_created(_engine_for(db_path), _metadata)


def record_generated_checks(
    db_path: Path,
    *,
    pr_number: int,
    head_sha: str,
    checks: Sequence[GeneratedCheck],
    timing: GeneratedTiming | None = None,
) -> None:
    """Replace the ``generated_checks`` rows of ``head_sha`` by ``checks``."""
    init_generated_schema(db_path)
    now = datetime.now(UTC).isoformat()
    engine = _engine_for(db_path)
    with engine.begin() as conn:
        conn.execute(
            delete(generated_checks).where(
                generated_checks.c.pr_number == pr_number,
                generated_checks.c.head_sha == head_sha,
            )
        )
        for check in checks:
            conn.execute(
                insert(generated_checks).values(
                    pr_number=pr_number,
                    head_sha=head_sha,
                    path=check.path,
                    argv=json.dumps(list(check.argv)),
                    exit_code=check.exit_code,
                    duration_ms=check.duration_ms,
                    output_tail=check.output_tail,
                    state=check.state.value,
                    script_ref=check.script_ref,
                    runs_for_path=check.runs_for_path,
                    sources_changed=json.dumps(list(check.sources_changed)),
                    producer_inputs_changed=json.dumps(list(check.producer_inputs_changed)),
                    artefact_sha256=check.artefact_sha256,
                    checks_ms=None if timing is None else timing.checks_ms,
                    overhead_ms=None if timing is None else timing.overhead_ms,
                    recorded_at=now,
                )
            )


def fetch_generated_checks(
    db_path: Path, pr_number: int, head_sha: str
) -> tuple[list[GeneratedCheck], GeneratedTiming | None]:
    """Return the runs recorded at ``head_sha`` and the timing they came with."""
    init_generated_schema(db_path)
    engine = _engine_for(db_path)
    with engine.connect() as conn:
        rows = conn.execute(
            select(generated_checks)
            .where(
                generated_checks.c.pr_number == pr_number,
                generated_checks.c.head_sha == head_sha,
            )
            .order_by(generated_checks.c.id)
        ).fetchall()
    checks = [
        GeneratedCheck(
            path=row.path,
            argv=tuple(json.loads(row.argv)),
            head_sha=row.head_sha,
            artefact_sha256=row.artefact_sha256,
            sources_changed=tuple(json.loads(row.sources_changed)),
            state=GeneratedState(row.state),
            exit_code=row.exit_code,
            duration_ms=row.duration_ms,
            output_tail=row.output_tail,
            script_ref="base" if row.script_ref == "base" else "head",
            producer_inputs_changed=tuple(json.loads(row.producer_inputs_changed)),
            runs_for_path=row.runs_for_path,
        )
        for row in rows
    ]
    timing = None
    if rows and rows[0].checks_ms is not None and rows[0].overhead_ms is not None:
        timing = GeneratedTiming(rows[0].checks_ms, rows[0].overhead_ms)
    return checks, timing


def settlements_at_head(db_path: Path, pr_number: int, head_sha: str) -> dict[str, ReverifyOutcome]:
    """Return how the ``generated_checks`` rows of ``head_sha`` settle each recorded path (G3, G6).

    ``resolved`` when every run of the path verified the derivation (a
    source changed, exit 0), ``still_open`` with the cause otherwise.
    """
    if not head_sha:
        return {}
    checks, _ = fetch_generated_checks(db_path, pr_number, head_sha)
    settled: dict[str, ReverifyOutcome] = {}
    for path, runs in by_path(checks).items():
        if path_verified(runs):
            reason = f"declared generated at {head_sha[:7]}, verified by {' '.join(runs[0].argv)}"
            settled[path] = ("resolved", GENERATED_FINDER, reason)
        else:
            causes = "; ".join(
                check_cause(r) for r in runs if r.state is not GeneratedState.VERIFIED
            )
            settled[path] = ("still_open", GENERATED_FINDER, causes)
    return settled


__all__ = [
    "fetch_generated_checks",
    "generated_checks",
    "init_generated_schema",
    "record_generated_checks",
    "settlements_at_head",
]
