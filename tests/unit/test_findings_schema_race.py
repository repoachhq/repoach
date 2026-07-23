"""Concurrency regression tests for ``init_findings_schema`` (SP-FINDINGS-INIT-RACE).

Reproduces the exact incident: ``_metadata.create_all(engine,
checkfirst=True)`` runs a check-then-create sequence that is not atomic
across independent SQLite connections, so N concurrent callers racing the
very first creation of ``pr_findings`` / ``pr_review_integrity`` can each
observe "missing" before any of them commits, and every loser then sees
the winner's ``CREATE TABLE`` as ``OperationalError: table ... already
exists``. The catch-and-verify fix in ``init_findings_schema`` swallows
that specific race while still re-raising a genuine, unrelated database
failure.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError

from repoach.review.findings import _engine_for, init_findings_schema

N_THREADS = 8


def test_concurrent_init_findings_schema_no_operational_error(tmp_path: Path) -> None:
    """Eight threads racing the first creation of a fresh db_path never raise.

    On the pre-fix code this reliably raises ``sqlite3.OperationalError:
    table pr_findings already exists`` in at least one of the eight
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
            init_findings_schema(db_path)
        except BaseException as exc:
            with errors_lock:
                errors.append(exc)

    with ThreadPoolExecutor(max_workers=N_THREADS) as pool:
        list(pool.map(lambda _: _call(), range(N_THREADS)))

    assert errors == []


def test_concurrent_init_findings_schema_all_threads_see_table_after_race(
    tmp_path: Path,
) -> None:
    """After the concurrent race, both tables exist with every current column.

    Regardless of which thread's ``create_all`` actually won the race,
    ``pr_findings`` must carry ``verify_attempts`` (the post-creation
    migration) once every thread has returned.
    """
    db_path = tmp_path / "race.db"
    barrier = threading.Barrier(N_THREADS)

    def _call() -> None:
        barrier.wait()
        init_findings_schema(db_path)

    with ThreadPoolExecutor(max_workers=N_THREADS) as pool:
        list(pool.map(lambda _: _call(), range(N_THREADS)))

    engine = _engine_for(db_path)
    inspector = inspect(engine)
    assert inspector.has_table("pr_findings")
    assert inspector.has_table("pr_review_integrity")
    columns = {col["name"] for col in inspector.get_columns("pr_findings")}
    assert "verify_attempts" in columns


def test_init_findings_schema_reraises_genuine_operational_error_when_table_absent(
    tmp_path: Path,
) -> None:
    """A genuine database failure unrelated to the race still propagates.

    Pointing ``db_path`` at a directory rather than a file makes every
    SQLite operation against it fail; since neither table can ever come
    to exist, the catch-and-verify re-check must not swallow the
    resulting ``OperationalError``.
    """
    bogus_db_path = tmp_path / "not_a_file"
    bogus_db_path.mkdir()

    with pytest.raises(OperationalError):
        init_findings_schema(bogus_db_path)
