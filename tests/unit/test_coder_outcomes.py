"""Unit tests for the per-model Coder outcome harvest (SP-CHAINPILOT-CODER-OUTCOMES)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select

from ferova.review.coder_outcomes import CoderModelOutcome, harvest_coder_outcomes
from ferova.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    init_findings_schema,
    record_finding,
)
from ferova.review.persistence import (
    _engine_for,
    _pr_coder_responses,
    init_schema,
    record_coder_response,
    record_merge,
)
from ferova.review.stuck import pr_coder_rounds, record_coder_round


def _coder(db: Path, *, pr_number: int, model: str) -> None:
    """Seed one Coder fix-plan row attributing ``pr_number`` to ``model``."""
    init_schema(db)
    record_coder_response(
        db,
        pr_number=pr_number,
        plan={"fixes": [], "commit_message": "c", "summary": "s"},
        model_used=model,
        elapsed_s=1.0,
        tokens_used=10,
    )


def _merged(db: Path, *, pr_number: int, outcome: str = "APPROVE") -> None:
    """Seed one green-CI merge outcome for ``pr_number``."""
    record_merge(
        db,
        pr_number=pr_number,
        outcome=outcome,
        base_ref="develop",
        head_ref=f"feat/pr-{pr_number}",
        merged_sha="deadbeef",
        notes="",
    )


def _rounds(db: Path, *, pr_number: int, count: int) -> None:
    """Append ``count`` Coder-round rows for ``pr_number``."""
    for _ in range(count):
        record_coder_round(
            db,
            pr_number=pr_number,
            open_blocking_before=1,
            open_blocking_after=0,
        )


def _stuck_finding(db: Path, *, pr_number: int) -> None:
    """Seed one terminal blocking STUCK finding for ``pr_number``."""
    init_findings_schema(db)
    record_finding(
        db,
        Finding(
            pr_number=pr_number,
            head_sha="head123",
            round=1,
            finder="architect",
            claim_type=ClaimType.DESIGN,
            severity=Severity.BLOCKING,
            file="src/m.py",
            line_start=1,
            line_end=1,
            claim="unresolved blocker",
            evidence_pointer="src/m.py:1",
            status=FindingStatus.STUCK,
        ),
    )


def _by_model(outcomes: list[CoderModelOutcome]) -> dict[str, CoderModelOutcome]:
    return {o.model: o for o in outcomes}


def test_empty_db_yields_empty_list(tmp_path: Path) -> None:
    assert harvest_coder_outcomes(tmp_path / "review.db") == []


def test_missing_db_path_does_not_raise(tmp_path: Path) -> None:
    nested = tmp_path / "never" / "initialised" / "review.db"
    assert harvest_coder_outcomes(nested) == []


def test_nominal_case_all_fields(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    for pr in (1, 2, 3, 4):
        _coder(db, pr_number=pr, model="deepseek-v4-pro")
    _merged(db, pr_number=1)
    _merged(db, pr_number=2, outcome="ALREADY_MERGED")
    _merged(db, pr_number=3)
    _rounds(db, pr_number=1, count=1)
    _rounds(db, pr_number=2, count=2)
    _rounds(db, pr_number=3, count=2)
    _stuck_finding(db, pr_number=4)

    outcome = _by_model(harvest_coder_outcomes(db))["deepseek-v4-pro"]
    assert outcome.n_prs == 4
    assert outcome.n_ci_green == 3
    assert outcome.ci_green_rate == 0.75
    assert outcome.avg_rounds_to_green == (1 + 2 + 2) / 3
    assert outcome.n_stuck == 1
    assert outcome.stuck_rate == 0.25


def test_no_green_prs(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    _coder(db, pr_number=10, model="qwen3.5")
    _coder(db, pr_number=11, model="qwen3.5")

    outcome = _by_model(harvest_coder_outcomes(db))["qwen3.5"]
    assert outcome.n_prs == 2
    assert outcome.n_ci_green == 0
    assert outcome.ci_green_rate == 0.0
    assert outcome.avg_rounds_to_green is None
    assert outcome.stuck_rate == 0.0


def test_multi_model_pr_contributes_to_both(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    _coder(db, pr_number=5, model="model-a")
    _coder(db, pr_number=5, model="model-b")
    _merged(db, pr_number=5)
    _rounds(db, pr_number=5, count=1)

    by_model = _by_model(harvest_coder_outcomes(db))
    assert by_model["model-a"].n_prs == 1
    assert by_model["model-a"].n_ci_green == 1
    assert by_model["model-b"].n_prs == 1
    assert by_model["model-b"].n_ci_green == 1


def test_merged_pr_without_rounds(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    _coder(db, pr_number=7, model="model-c")
    _merged(db, pr_number=7)

    outcome = _by_model(harvest_coder_outcomes(db))["model-c"]
    assert outcome.n_ci_green == 1
    assert outcome.avg_rounds_to_green is None


def test_outcomes_ordered_by_model_id(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    _coder(db, pr_number=1, model="zeta")
    _coder(db, pr_number=2, model="alpha")
    _coder(db, pr_number=3, model="mu")

    assert [o.model for o in harvest_coder_outcomes(db)] == ["alpha", "mu", "zeta"]


def test_harvest_is_read_only(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    _coder(db, pr_number=1, model="model-a")
    _merged(db, pr_number=1)
    _rounds(db, pr_number=1, count=2)
    _stuck_finding(db, pr_number=1)

    engine = _engine_for(db)
    with engine.connect() as conn:
        before_responses = conn.execute(
            select(func.count()).select_from(_pr_coder_responses)
        ).scalar_one()
        before_rounds = conn.execute(select(func.count()).select_from(pr_coder_rounds)).scalar_one()

    harvest_coder_outcomes(db)

    with engine.connect() as conn:
        after_responses = conn.execute(
            select(func.count()).select_from(_pr_coder_responses)
        ).scalar_one()
        after_rounds = conn.execute(select(func.count()).select_from(pr_coder_rounds)).scalar_one()

    assert (before_responses, before_rounds) == (after_responses, after_rounds)
