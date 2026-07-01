"""Tests for L4 persistence + archive rendering of hallucination events.

SP-DEV-V2-B4 — surfaces the GuardEvent stream from
``apply_hallucination_guard`` into a queryable L4 table
(``pr_hallucinations``) and a human-readable Guard activity section
in the sticky archive comment.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text

from ferova.review.hallucination_guard import GuardEvent
from ferova.review.orchestrator import (
    TeamOutcome,
    _render_guard_section,
    team_outcome_to_dict,
)
from ferova.review.persistence import (
    init_schema,
    record_hallucination,
)
from ferova.review.reviewer import (
    BotRole,
    ReviewerOutcome,
    ReviewVerdict,
)


def _event(
    *,
    role: BotRole = BotRole.ARCHITECT,
    reason: str = "self_referential",
    file: str = "prompts/review/architect_0.1.0.md",
    line: int = 80,
    tokens: tuple[str, ...] = (),
    body: str = "The prompt incorrectly includes the full plan.",
) -> GuardEvent:
    return GuardEvent(
        role=role,
        file=file,
        line=line,
        original_severity="major",
        reason=reason,
        tokens_found=tokens,
        original_body=body,
    )


def test_record_hallucination_round_trip(tmp_path: Path):
    db_path = tmp_path / "l4.sqlite"
    init_schema(db_path)

    record_hallucination(db_path, pr_number=20, event=_event())
    record_hallucination(
        db_path,
        pr_number=20,
        event=_event(
            role=BotRole.SCRIBE,
            reason="missing_token_found_in_file",
            file="src/ferova/foo.py",
            line=42,
            tokens=("Args",),
            body="Args section is missing on respond.",
        ),
    )

    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT pr_number, role, file, line, original_severity, reason, tokens_found "
                "FROM pr_hallucinations ORDER BY id"
            )
        ).all()

    assert len(rows) == 2
    assert rows[0][1] == "architect"
    assert rows[0][5] == "self_referential"
    assert rows[1][1] == "scribe"
    assert "Args" in rows[1][6]


def test_team_outcome_to_dict_includes_hallucinations():
    review = ReviewerOutcome(
        role=BotRole.ARCHITECT,
        verdict=ReviewVerdict.COMMENT,
        summary="ok",
        comments=[],
        model_used="qwen/qwen3-next-80b-a3b-instruct",
        elapsed_s=0.1,
        tokens_used=64,
        raw_response="{}",
    )
    team = TeamOutcome(
        pr_number=20,
        final_verdict=ReviewVerdict.COMMENT,
        n_blockers=0,
        n_majors=0,
        reviews=[review],
        hallucinations=[_event()],
    )

    payload = team_outcome_to_dict(team)

    assert payload["n_hallucinations_downgraded"] == 1
    assert payload["hallucinations"][0]["role"] == "architect"
    assert payload["hallucinations"][0]["reason"] == "self_referential"


def test_render_guard_section_empty_when_no_events():
    team = TeamOutcome(
        pr_number=20,
        final_verdict=ReviewVerdict.APPROVE,
        n_blockers=0,
        n_majors=0,
    )
    assert _render_guard_section(team) == ""


def test_render_guard_section_lists_events_with_breakdown():
    team = TeamOutcome(
        pr_number=20,
        final_verdict=ReviewVerdict.COMMENT,
        n_blockers=0,
        n_majors=0,
        hallucinations=[
            _event(),
            _event(role=BotRole.ARCHITECT, line=120),
            _event(
                role=BotRole.SCRIBE,
                reason="missing_token_found_in_file",
                file="src/ferova/foo.py",
                line=42,
                tokens=("Args", "Returns"),
            ),
        ],
    )

    section = _render_guard_section(team)

    assert "3 hallucination" in section
    assert "architect: 2" in section
    assert "scribe: 1" in section
    assert "Args, Returns" in section
    assert "prompts/review/architect_0.1.0.md:80" in section
