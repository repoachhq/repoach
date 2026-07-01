"""Unit tests for the per-model performance scoreboard (SP-CHAINPILOT-PERF-AGGREGATE)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import insert

from ferova.llm_proxy.providers.benchmark_equivalences import (
    EquivalenceTable,
    ModelEquivalence,
)
from ferova.llm_proxy.providers.benchmark_prior import (
    BenchmarkEntry,
    BenchmarkRanking,
    BenchmarkSourceMeta,
)
from ferova.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    init_findings_schema,
    record_finding,
)
from ferova.review.perf_aggregate import (
    GuardedMetric,
    ModelPerformance,
    aggregate_model_performance,
)
from ferova.review.persistence import (
    _engine_for,
    _pr_reviews,
    init_schema,
    record_coder_response,
    record_merge,
)
from ferova.review.stuck import record_coder_round


def _coder(db: Path, *, pr_number: int, model: str, merged: bool, rounds: int) -> None:
    init_schema(db)
    record_coder_response(
        db,
        pr_number=pr_number,
        plan={"fixes": [], "commit_message": "c", "summary": "s"},
        model_used=model,
        elapsed_s=1.0,
        tokens_used=10,
    )
    if merged:
        record_merge(
            db,
            pr_number=pr_number,
            outcome="APPROVE",
            base_ref="develop",
            head_ref=f"feat/pr-{pr_number}",
            merged_sha="sha",
            notes="",
        )
    for _ in range(rounds):
        record_coder_round(db, pr_number=pr_number, open_blocking_before=1, open_blocking_after=0)


def _review(db: Path, *, pr_number: int, model: str) -> None:
    init_schema(db)
    engine = _engine_for(db)
    with engine.begin() as conn:
        conn.execute(
            insert(_pr_reviews).values(
                pr_number=pr_number,
                role="architect",
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


def _finding(db: Path, *, pr_number: int, status: FindingStatus, file: str) -> None:
    init_findings_schema(db)
    record_finding(
        db,
        Finding(
            pr_number=pr_number,
            head_sha="h",
            round=1,
            finder="architect",
            claim_type=ClaimType.DESIGN,
            severity=Severity.BLOCKING,
            file=file,
            line_start=1,
            line_end=1,
            claim="c",
            evidence_pointer=f"{file}:1",
            status=status,
        ),
    )


def _ranking() -> BenchmarkRanking:
    return BenchmarkRanking(
        sources=(
            BenchmarkSourceMeta(
                source="aa", url="u", captured="2026-01-01", metric="intel", scale="0-100"
            ),
        ),
        entries=(
            BenchmarkEntry(
                source="aa", model_name="DeepSeek V4", metric="intel", score=80.0, rank=2
            ),
        ),
    )


def _equivalences() -> EquivalenceTable:
    return EquivalenceTable(
        equivalences=(
            ModelEquivalence(
                canonical="deepseek-v4",
                aliases=("DeepSeek V4",),
                id_patterns=("deepseek-v4",),
            ),
        )
    )


def _metrics(perf: ModelPerformance) -> dict[str, GuardedMetric]:
    return {m.metric: m for m in perf.metrics}


def _by_model(perfs: list[ModelPerformance]) -> dict[str, ModelPerformance]:
    return {p.model: p for p in perfs}


def test_empty_db_yields_empty_list(tmp_path: Path) -> None:
    assert aggregate_model_performance(tmp_path / "review.db") == []


def test_coder_only_model_carries_three_coder_dimensions(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    for pr in range(1, 7):
        _coder(db, pr_number=pr, model="deepseek-ai/deepseek-v4-pro", merged=pr <= 4, rounds=1)

    perf = _by_model(aggregate_model_performance(db))["deepseek-ai/deepseek-v4-pro"]
    metrics = _metrics(perf)
    assert set(metrics) == {"coder_ci_green", "coder_stuck", "coder_rounds_to_green"}
    assert metrics["coder_ci_green"].value == 4 / 6
    assert metrics["coder_ci_green"].n == 6
    assert metrics["coder_ci_green"].confident is True


def test_reviewer_only_model_carries_precision(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    _review(db, pr_number=1, model="mistral-medium")
    _finding(db, pr_number=1, status=FindingStatus.VERIFIED, file="src/a.py")
    _finding(db, pr_number=1, status=FindingStatus.REFUTED, file="src/b.py")

    perf = _by_model(aggregate_model_performance(db))["mistral-medium"]
    metrics = _metrics(perf)
    assert set(metrics) == {"reviewer_precision"}
    assert metrics["reviewer_precision"].value == 0.5
    assert metrics["reviewer_precision"].n == 2


def test_min_sample_guard(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    for pr in range(1, 4):
        _coder(db, pr_number=pr, model="m", merged=True, rounds=1)

    perf = _by_model(aggregate_model_performance(db, min_sample=5))["m"]
    assert _metrics(perf)["coder_ci_green"].confident is False
    perf_lenient = _by_model(aggregate_model_performance(db, min_sample=3))["m"]
    assert _metrics(perf_lenient)["coder_ci_green"].confident is True


def test_prior_attached_when_inputs_present(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    _coder(db, pr_number=1, model="deepseek-ai/deepseek-v4-pro", merged=True, rounds=1)

    perf = _by_model(
        aggregate_model_performance(
            db, ranking=_ranking(), equivalences=_equivalences(), prior_metric="intel"
        )
    )["deepseek-ai/deepseek-v4-pro"]
    assert perf.prior_rank == 2
    assert perf.prior_score == 80.0


def test_prior_none_when_inputs_absent(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    _coder(db, pr_number=1, model="deepseek-ai/deepseek-v4-pro", merged=True, rounds=1)

    perf = _by_model(
        aggregate_model_performance(db, ranking=_ranking(), equivalences=_equivalences())
    )["deepseek-ai/deepseek-v4-pro"]
    assert perf.prior_rank is None
    assert perf.prior_score is None


def test_prior_none_for_unmatched_model(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    _coder(db, pr_number=1, model="some/unknown-model", merged=True, rounds=1)

    perf = _by_model(
        aggregate_model_performance(
            db, ranking=_ranking(), equivalences=_equivalences(), prior_metric="intel"
        )
    )["some/unknown-model"]
    assert perf.prior_rank is None


def test_prior_skips_rankless_attribute_metric(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    _coder(db, pr_number=1, model="deepseek-ai/deepseek-v4-pro", merged=True, rounds=1)

    ranking = BenchmarkRanking(
        sources=(
            BenchmarkSourceMeta(
                source="aa_speed",
                url="u",
                captured="2026-06-26",
                metric="output_speed",
                scale="tok/s",
            ),
        ),
        entries=(
            BenchmarkEntry(
                source="aa_speed", model_name="DeepSeek V4", metric="output_speed", score=78.7
            ),
        ),
    )

    perf = _by_model(
        aggregate_model_performance(
            db, ranking=ranking, equivalences=_equivalences(), prior_metric="output_speed"
        )
    )["deepseek-ai/deepseek-v4-pro"]
    assert perf.prior_rank is None
    assert perf.prior_score is None


def test_union_ordered_by_model_id(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    _coder(db, pr_number=1, model="zeta-coder", merged=True, rounds=1)
    _review(db, pr_number=2, model="alpha-reviewer")
    _finding(db, pr_number=2, status=FindingStatus.VERIFIED, file="src/a.py")

    assert [p.model for p in aggregate_model_performance(db)] == ["alpha-reviewer", "zeta-coder"]
