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

The thread-pool tests above reproduce the incident by wall-clock luck,
which is why the underlying bug shipped and stayed flaky under CI load
without a hard failure locally. ``test_create_all_with_retries_converges_after_documented_multitable_interleaving``
below forces the exact documented interleaving deterministically --
no barrier, no timing, no flake -- by making the first ``create_all``
attempt commit ``pr_findings`` for real and then fail with a genuine
"already exists" ``OperationalError`` while ``pr_review_integrity``
is still absent, which is precisely the multi-table window the
original single-shot re-check got wrong.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import MetaData, inspect
from sqlalchemy.exc import OperationalError

from repoach.review.findings import (
    _create_all_with_retries,
    _engine_for,
    init_findings_schema,
    pr_findings,
)

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


def test_create_all_with_retries_converges_after_documented_multitable_interleaving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deterministically forces the documented multi-table interleaving.

    Reproducing SP-FINDINGS-INIT-RACE-MULTITABLE by wall-clock thread
    timing is inherently unreliable -- the window is the few
    instructions between a losing thread's failed ``CREATE
    pr_findings`` and the winning thread's not-yet-executed ``CREATE
    pr_review_integrity``. This test removes the timing dependency
    entirely: it patches ``MetaData.create_all`` so the *first* call
    commits ``pr_findings`` for real and then re-attempts creating it
    with ``checkfirst=False``, which SQLite genuinely rejects with
    ``OperationalError: table pr_findings already exists`` -- while
    ``pr_review_integrity`` has not been created by anyone. That is
    exactly the state the original single-shot re-check saw and
    wrongly re-raised on. ``_create_all_with_retries`` must instead
    retry, and its second (unpatched) attempt converges because
    ``checkfirst=True`` skips the now-existing ``pr_findings`` and
    creates only the still-missing ``pr_review_integrity``.
    """
    db_path = tmp_path / "race.db"
    engine = _engine_for(db_path)
    real_create_all = MetaData.create_all
    attempts = {"n": 0}

    def _fake_create_all(self: MetaData, bind: object, checkfirst: bool = True) -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            pr_findings.create(bind, checkfirst=True)
            pr_findings.create(bind, checkfirst=False)
        else:
            real_create_all(self, bind, checkfirst=checkfirst)

    monkeypatch.setattr(MetaData, "create_all", _fake_create_all)

    _create_all_with_retries(engine)

    inspector = inspect(engine)
    assert inspector.has_table("pr_findings")
    assert inspector.has_table("pr_review_integrity")
    assert attempts["n"] == 2
