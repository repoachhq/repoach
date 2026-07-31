"""Concurrency regression test for ``init_stuck_schema`` (SP-SCHEMA-INIT-RACE-GENERALIZE).

Mirrors ``tests/unit/test_findings_schema_race.py``'s barrier-based
technique, but targets a previously-unprotected sibling store rather
than ``pr_findings``: eight threads race the very first creation of
``pr_coder_rounds`` against the same fresh ``db_path``. On pre-fix
``stuck.py`` (a raw, unprotected ``_metadata.create_all(engine,
checkfirst=True)``) this reliably surfaces ``OperationalError: table
pr_coder_rounds already exists`` in at least one thread, since
``checkfirst=True`` is a non-atomic check-then-create across
independent SQLite connections. After routing ``init_stuck_schema``
through the shared ``ensure_schema_created`` helper, the race is
absorbed and no thread ever observes the transient failure.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import inspect

from repoach.review.stuck import _engine_for, init_stuck_schema

N_THREADS = 8


def test_concurrent_init_stuck_schema_no_operational_error(tmp_path: Path) -> None:
    """Eight threads racing the first creation of a fresh db_path never raise.

    On the pre-fix code this reliably raises ``sqlite3.OperationalError:
    table pr_coder_rounds already exists`` in at least one of the eight
    threads, since ``checkfirst=True`` is a non-atomic check-then-create
    across independent SQLite connections.
    """
    db_path = tmp_path / "race.db"
    barrier = threading.Barrier(N_THREADS)
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def _call() -> None:
        barrier.wait()
        try:
            init_stuck_schema(db_path)
        except BaseException as exc:
            with errors_lock:
                errors.append(exc)

    with ThreadPoolExecutor(max_workers=N_THREADS) as pool:
        list(pool.map(lambda _: _call(), range(N_THREADS)))

    assert errors == []

    engine = _engine_for(db_path)
    inspector = inspect(engine)
    assert inspector.has_table("pr_coder_rounds")
