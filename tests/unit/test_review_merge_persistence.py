"""Migration tests for SP-RETIRE-VERDICT-ARCHIVE 10b-4 — pr_merges.verdict drop.

A pre-10b-4 database carries ``pr_merges.verdict`` as ``NOT NULL``. Since
``record_merge`` no longer supplies a verdict, ``init_schema`` must drop
the column on an existing database; otherwise the next insert would trip
the ``NOT NULL`` constraint. These tests pin the drop migration and the
post-migration insert path.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from repoach.review.persistence import init_schema, record_merge


def _legacy_pr_merges(db: Path) -> None:
    """Materialise a pre-10b-4 ``pr_merges`` with a NOT NULL verdict column."""
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE pr_merges ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "pr_number INTEGER NOT NULL, "
                "outcome TEXT NOT NULL, "
                "base_ref TEXT NOT NULL, "
                "head_ref TEXT NOT NULL, "
                "verdict TEXT NOT NULL, "
                "merged_sha TEXT, "
                "notes TEXT NOT NULL, "
                "created_at TIMESTAMP NOT NULL)"
            )
        )


def _columns(db: Path) -> set[str]:
    engine = create_engine(f"sqlite:///{db}")
    return {col["name"] for col in inspect(engine).get_columns("pr_merges")}


def test_init_schema_drops_legacy_verdict_column(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    _legacy_pr_merges(db)
    init_schema(db)
    assert "verdict" not in _columns(db)


def test_record_merge_works_after_drop(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    _legacy_pr_merges(db)
    init_schema(db)
    record_merge(
        db,
        pr_number=1,
        outcome="APPROVE",
        base_ref="develop",
        head_ref="feat/x",
        merged_sha="abc",
        notes="ok",
    )
    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM pr_merges")).scalar()
    assert count == 1


def test_init_schema_idempotent_on_fresh_db(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    init_schema(db)
    init_schema(db)
    record_merge(
        db,
        pr_number=2,
        outcome="SKIP_GATE",
        base_ref="develop",
        head_ref="feat/y",
        merged_sha=None,
        notes="refused",
    )
    assert "verdict" not in _columns(db)
