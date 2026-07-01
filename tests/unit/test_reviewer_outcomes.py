"""Unit tests for the per-model reviewer precision harvest (SP-CHAINPILOT-REVIEWER-OUTCOMES)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import insert

from ferova.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    init_findings_schema,
    record_finding,
)
from ferova.review.persistence import _engine_for, _pr_reviews, init_schema
from ferova.review.reviewer_outcomes import (
    ReviewerModelOutcome,
    harvest_reviewer_outcomes,
)


def _review(db: Path, *, pr_number: int, role: str, model: str) -> None:
    """Seed one pr_reviews row attributing ``role`` on ``pr_number`` to ``model``."""
    init_schema(db)
    engine = _engine_for(db)
    with engine.begin() as conn:
        conn.execute(
            insert(_pr_reviews).values(
                pr_number=pr_number,
                role=role,
                verdict="APPROVE",
                summary="s",
                n_comments=0,
                n_blockers=0,
                n_majors=0,
                model_used=model,
                elapsed_s=1.0,
                tokens_used=10,
                comments_json="[]",
                created_at=datetime.now(UTC),
            )
        )


def _finding(
    db: Path,
    *,
    pr_number: int,
    finder: str,
    status: FindingStatus,
    file: str = "src/m.py",
) -> None:
    """Seed one finding raised by ``finder`` on ``pr_number`` with ``status``."""
    init_findings_schema(db)
    record_finding(
        db,
        Finding(
            pr_number=pr_number,
            head_sha="head123",
            round=1,
            finder=finder,
            claim_type=ClaimType.DESIGN,
            severity=Severity.BLOCKING,
            file=file,
            line_start=1,
            line_end=1,
            claim="claim",
            evidence_pointer=f"{file}:1",
            status=status,
        ),
    )


def _by_model(outcomes: list[ReviewerModelOutcome]) -> dict[str, ReviewerModelOutcome]:
    return {o.model: o for o in outcomes}


def test_empty_db_yields_empty_list(tmp_path: Path) -> None:
    assert harvest_reviewer_outcomes(tmp_path / "review.db") == []


def test_nominal_precision(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    _review(db, pr_number=1, role="architect", model="model-x")
    _review(db, pr_number=2, role="architect", model="model-x")
    _finding(db, pr_number=1, finder="architect", status=FindingStatus.VERIFIED, file="src/a.py")
    _finding(db, pr_number=1, finder="architect", status=FindingStatus.RESOLVED, file="src/b.py")
    _finding(db, pr_number=2, finder="architect", status=FindingStatus.OPEN, file="src/c.py")
    _finding(db, pr_number=2, finder="architect", status=FindingStatus.REFUTED, file="src/d.py")

    outcome = _by_model(harvest_reviewer_outcomes(db))["model-x"]
    assert outcome.confirmed == 3
    assert outcome.refuted == 1
    assert outcome.n_settled == 4
    assert outcome.precision == 0.75


def test_only_proposed_findings_have_none_precision(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    _review(db, pr_number=1, role="sentinel", model="model-y")
    _finding(db, pr_number=1, finder="sentinel", status=FindingStatus.PROPOSED)

    outcome = _by_model(harvest_reviewer_outcomes(db))["model-y"]
    assert outcome.n_settled == 0
    assert outcome.precision is None


def test_multi_model_role_contributes_to_both(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    _review(db, pr_number=1, role="tester", model="model-a")
    _review(db, pr_number=1, role="tester", model="model-b")
    _finding(db, pr_number=1, finder="tester", status=FindingStatus.VERIFIED)

    by_model = _by_model(harvest_reviewer_outcomes(db))
    assert by_model["model-a"].confirmed == 1
    assert by_model["model-b"].confirmed == 1


def test_ci_and_unattributed_findings_excluded(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    _review(db, pr_number=1, role="architect", model="model-x")
    _finding(db, pr_number=1, finder="architect", status=FindingStatus.VERIFIED, file="src/a.py")
    _finding(db, pr_number=1, finder="ci", status=FindingStatus.VERIFIED, file="src/b.py")
    _finding(db, pr_number=9, finder="scribe", status=FindingStatus.VERIFIED, file="src/c.py")

    outcomes = harvest_reviewer_outcomes(db)
    assert [o.model for o in outcomes] == ["model-x"]
    assert outcomes[0].confirmed == 1


def test_outcomes_ordered_by_model_id(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    _review(db, pr_number=1, role="architect", model="zeta")
    _review(db, pr_number=2, role="architect", model="alpha")
    _finding(db, pr_number=1, finder="architect", status=FindingStatus.VERIFIED, file="src/a.py")
    _finding(db, pr_number=2, finder="architect", status=FindingStatus.VERIFIED, file="src/b.py")

    assert [o.model for o in harvest_reviewer_outcomes(db)] == ["alpha", "zeta"]
