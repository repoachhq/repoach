"""Unit tests for the injectable ``recorded_at`` on persistence.py writers.

SP-REVIEW-PERSIST-RECORDED-AT — each of the five `persistence.py` writers
(``record_review``, ``record_merge``, ``record_hallucination``,
``record_dialogue``, ``record_coder_response``) now accepts an optional
keyword-only ``recorded_at: datetime | None = None`` so a caller (or a
test) can pin the persisted ``created_at`` to a known value instead of
relying on the writer's internal ``datetime.now(UTC)``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, text

from repoach.review.hallucination_guard import GuardEvent
from repoach.review.persistence import (
    init_schema,
    record_coder_response,
    record_dialogue,
    record_hallucination,
    record_merge,
    record_review,
)
from repoach.review.reviewer import BotRole, ReviewerOutcome, ReviewVerdict

FIXED_RECORDED_AT = datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)


def _outcome() -> ReviewerOutcome:
    return ReviewerOutcome(
        role=BotRole.ARCHITECT,
        verdict=ReviewVerdict.APPROVE,
        summary="looks fine",
        comments=[],
        model_used="kimi-k2-instruct",
        elapsed_s=0.2,
        tokens_used=100,
    )


def _guard_event() -> GuardEvent:
    return GuardEvent(
        role=BotRole.SENTINEL,
        file="src/repoach/foo.py",
        line=10,
        original_severity="major",
        reason="self_referential",
        tokens_found=(),
        original_body="original comment body",
    )


def _created_at(db_path: Path, table: str) -> datetime:
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.connect() as conn:
        row = conn.execute(text(f"SELECT created_at FROM {table} ORDER BY id DESC LIMIT 1")).one()
    value = row[0]
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value


def test_record_review_accepts_injected_recorded_at(tmp_path: Path) -> None:
    db_path = tmp_path / "l4.sqlite"
    init_schema(db_path)

    record_review(
        db_path,
        pr_number=1,
        outcome=_outcome(),
        recorded_at=FIXED_RECORDED_AT,
    )

    assert _created_at(db_path, "pr_reviews") == FIXED_RECORDED_AT


def test_record_merge_accepts_injected_recorded_at(tmp_path: Path) -> None:
    db_path = tmp_path / "l4.sqlite"
    init_schema(db_path)

    record_merge(
        db_path,
        pr_number=1,
        outcome="APPROVE",
        base_ref="develop",
        head_ref="feat/x",
        merged_sha="abc123",
        notes="ok",
        recorded_at=FIXED_RECORDED_AT,
    )

    assert _created_at(db_path, "pr_merges") == FIXED_RECORDED_AT


def test_record_hallucination_accepts_injected_recorded_at(tmp_path: Path) -> None:
    db_path = tmp_path / "l4.sqlite"
    init_schema(db_path)

    record_hallucination(
        db_path,
        pr_number=1,
        event=_guard_event(),
        recorded_at=FIXED_RECORDED_AT,
    )

    assert _created_at(db_path, "pr_hallucinations") == FIXED_RECORDED_AT


def test_record_dialogue_accepts_injected_recorded_at(tmp_path: Path) -> None:
    db_path = tmp_path / "l4.sqlite"
    init_schema(db_path)

    record_dialogue(
        db_path,
        pr_number=1,
        round="1",
        speaker="architect",
        payload={"verdict": "APPROVE"},
        recorded_at=FIXED_RECORDED_AT,
    )

    assert _created_at(db_path, "pr_review_dialogue") == FIXED_RECORDED_AT


def test_record_coder_response_accepts_injected_recorded_at(tmp_path: Path) -> None:
    db_path = tmp_path / "l4.sqlite"
    init_schema(db_path)

    record_coder_response(
        db_path,
        pr_number=1,
        plan={"fixes": [], "summary": "done", "commit_message": "fix: x"},
        model_used="kimi-k2-instruct",
        elapsed_s=1.5,
        tokens_used=500,
        recorded_at=FIXED_RECORDED_AT,
    )

    assert _created_at(db_path, "pr_coder_responses") == FIXED_RECORDED_AT


def test_all_five_writers_default_to_now_when_recorded_at_omitted(tmp_path: Path) -> None:
    db_path = tmp_path / "l4.sqlite"
    init_schema(db_path)

    before = datetime.now(UTC)
    record_review(db_path, pr_number=1, outcome=_outcome())
    record_merge(
        db_path,
        pr_number=1,
        outcome="APPROVE",
        base_ref="develop",
        head_ref="feat/x",
        merged_sha="abc123",
        notes="ok",
    )
    record_hallucination(db_path, pr_number=1, event=_guard_event())
    record_dialogue(
        db_path,
        pr_number=1,
        round="1",
        speaker="architect",
        payload={"verdict": "APPROVE"},
    )
    record_coder_response(
        db_path,
        pr_number=1,
        plan={"fixes": [], "summary": "done", "commit_message": "fix: x"},
        model_used="kimi-k2-instruct",
        elapsed_s=1.5,
        tokens_used=500,
    )
    after = datetime.now(UTC)

    for table in (
        "pr_reviews",
        "pr_merges",
        "pr_hallucinations",
        "pr_review_dialogue",
        "pr_coder_responses",
    ):
        stamped = _created_at(db_path, table)
        assert before <= stamped <= after
