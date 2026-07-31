"""Unit tests for the shared race-proof schema-init helper.

``SP-SCHEMA-INIT-RACE-GENERALIZE``. Mirrors
``tests/unit/test_findings_schema_race.py``'s ``_fake_create_all``
technique but exercises :func:`ensure_schema_created` directly against
an arbitrary ``MetaData`` rather than the ``pr_findings`` /
``pr_review_integrity`` pair, proving the retry/reraise contract is
generic and not hardcoded to any one schema.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect
from sqlalchemy.exc import OperationalError

from repoach.core.sqlite_schema_init import ensure_schema_created


def _build_metadata() -> tuple[MetaData, Table]:
    metadata = MetaData()
    table = Table(
        "widgets",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("name", String, nullable=False),
    )
    return metadata, table


def test_ensure_schema_created_converges_after_transient_operational_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two transient ``OperationalError`` retries still converge to success.

    Patches ``MetaData.create_all`` so the first two attempts raise
    ``OperationalError`` (simulating a losing concurrent ``CREATE
    TABLE``) and the third delegates to the real implementation.
    ``ensure_schema_created`` must retry through the failures and
    return without raising, leaving the declared table in place.
    """
    db_path = tmp_path / "widgets.db"
    engine = create_engine(f"sqlite:///{db_path}")
    metadata, _table = _build_metadata()
    real_create_all = MetaData.create_all
    attempts = {"n": 0}

    def _fake_create_all(self: MetaData, bind: object, checkfirst: bool = True) -> None:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise OperationalError(
                "CREATE TABLE widgets", {}, Exception("table widgets already exists")
            )
        real_create_all(self, bind, checkfirst=checkfirst)

    monkeypatch.setattr(MetaData, "create_all", _fake_create_all)

    ensure_schema_created(engine, metadata)

    inspector = inspect(engine)
    assert inspector.has_table("widgets")
    assert attempts["n"] == 3


def test_ensure_schema_created_reraises_after_exhausting_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persistently failing ``create_all`` still propagates once bounded retries exhaust.

    Patches ``MetaData.create_all`` to always raise ``OperationalError``
    and asserts ``ensure_schema_created`` re-raises rather than
    swallowing the failure -- the declared table genuinely never comes
    to exist, so this is not a resolved creation race.
    """
    db_path = tmp_path / "widgets.db"
    engine = create_engine(f"sqlite:///{db_path}")
    metadata, _table = _build_metadata()

    def _always_fails(self: MetaData, bind: object, checkfirst: bool = True) -> None:
        raise OperationalError(
            "CREATE TABLE widgets", {}, Exception("table widgets already exists")
        )

    monkeypatch.setattr(MetaData, "create_all", _always_fails)

    with pytest.raises(OperationalError):
        ensure_schema_created(engine, metadata)
