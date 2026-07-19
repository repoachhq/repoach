"""Round-2 dialogue tests (SP-REVIEW-DIALOGUE-A).

Verifies that the orchestrator:

- skips round 2 when no reviewer flagged a blocker / major,
- runs round 2 ONLY for reviewers that did flag,
- builds a :class:`DialogueContext` excluding the reviewer's own
  round-1 outcome from peer_outcomes,
- truncates each peer's summary to 200 chars,
- forwards the runner's hallucination-guard verdicts on the
  reviewer's own round-1 comments,
- attaches 30-line file excerpts around every cited ``file:line``,
- drops ``severity="retracted"`` comments from the final aggregation,
- softens REQUEST_CHANGES → COMMENT when every blocker / major
  retracts in round 2.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from repoach.review.hallucination_guard import GuardEvent
from repoach.review.orchestrator import (
    ReviewTeamOrchestrator,
    _drop_retracted_comments,
    _locate_comment_index,
)
from repoach.review.reviewer import (
    Architect,
    BotRole,
    DialogueContext,
    FileExcerpt,
    GuardEventSummary,
    PeerOutcome,
    ReviewComment,
    ReviewerOutcome,
    ReviewVerdict,
    Scribe,
    Sentinel,
    Tester,
    make_file_excerpts,
)


def _outcome(
    *,
    role: BotRole,
    verdict: ReviewVerdict,
    comments: list[ReviewComment] | None = None,
    summary: str = "summary",
) -> ReviewerOutcome:
    return ReviewerOutcome(
        role=role,
        verdict=verdict,
        summary=summary,
        comments=comments or [],
        model_used="test-model",
        elapsed_s=0.1,
        tokens_used=10,
        raw_response="{}",
    )


def _orchestrator(tmp_path):
    gh = MagicMock()
    return ReviewTeamOrchestrator(
        gh=gh,
        db_path=tmp_path / "l4.db",
        post_to_github=False,
    )


def _reviewer(role: BotRole):
    """A real Reviewer subclass instance with a stubbed AgentLoop."""
    cls_for_role = {
        BotRole.ARCHITECT: Architect,
        BotRole.SENTINEL: Sentinel,
        BotRole.TESTER: Tester,
        BotRole.SCRIBE: Scribe,
    }[role]
    return cls_for_role(loop=MagicMock())


# ---------------------------------------------------------------------------
# 1. round 2 skipped when no triggering outcome
# ---------------------------------------------------------------------------


def test_round_two_skipped_when_no_blockers_or_majors(tmp_path):
    orch = _orchestrator(tmp_path)
    reviewer_roles = [BotRole.ARCHITECT, BotRole.SENTINEL, BotRole.TESTER, BotRole.SCRIBE]
    reviewers = [_reviewer(r) for r in reviewer_roles]
    outcomes = [
        _outcome(role=r.role, verdict=ReviewVerdict.APPROVE, comments=[]) for r in reviewers
    ]
    revised = orch._round_two(
        reviewers=reviewers,
        round1_outcomes=outcomes,
        spec_plan=None,
        guard_events=[],
        diff="diff",
        pr_number=1,
    )
    assert revised == outcomes
    # No reviewer should have been called.
    for r in reviewers:
        r._loop.run_oneshot.assert_not_called()


# ---------------------------------------------------------------------------
# 2. only flagging reviewers re-run
# ---------------------------------------------------------------------------


def test_round_two_runs_only_for_flagging_reviewers(tmp_path, monkeypatch):
    orch = _orchestrator(tmp_path)
    reviewers = [_reviewer(BotRole.ARCHITECT), _reviewer(BotRole.SCRIBE)]
    blocker = ReviewComment(
        file="src/x.py",
        line=42,
        severity="blocker",
        body="something wrong",
    )
    round1 = [
        _outcome(role=BotRole.ARCHITECT, verdict=ReviewVerdict.APPROVE),
        _outcome(role=BotRole.SCRIBE, verdict=ReviewVerdict.REQUEST_CHANGES, comments=[blocker]),
    ]
    monkeypatch.setattr(
        reviewers[1],
        "review_diff",
        MagicMock(
            return_value=_outcome(role=BotRole.SCRIBE, verdict=ReviewVerdict.APPROVE),
        ),
    )
    arch_review = MagicMock(side_effect=AssertionError("Architect should not re-run"))
    monkeypatch.setattr(reviewers[0], "review_diff", arch_review)
    revised = orch._round_two(
        reviewers=reviewers,
        round1_outcomes=round1,
        spec_plan=None,
        guard_events=[],
        diff="diff",
        pr_number=1,
    )
    arch_review.assert_not_called()
    reviewers[1].review_diff.assert_called_once()
    assert revised[0] is round1[0]
    assert revised[1].verdict is ReviewVerdict.APPROVE


# ---------------------------------------------------------------------------
# 3. peer outcomes truncated + exclude self
# ---------------------------------------------------------------------------


def test_dialogue_context_excludes_self_and_truncates_peer_summary(tmp_path):
    orch = _orchestrator(tmp_path)
    long_summary = "a" * 500
    round1 = [
        _outcome(role=BotRole.ARCHITECT, verdict=ReviewVerdict.APPROVE, summary=long_summary),
        _outcome(
            role=BotRole.SCRIBE,
            verdict=ReviewVerdict.REQUEST_CHANGES,
            summary=long_summary,
            comments=[ReviewComment(file="x.py", line=1, severity="blocker", body="b")],
        ),
    ]
    ctx = orch._build_dialogue_context(
        reviewer_role=BotRole.SCRIBE,
        reviewer_outcome=round1[1],
        round1_outcomes=round1,
        guard_events=[],
    )
    # Self excluded.
    assert all(p.role is not BotRole.SCRIBE for p in ctx.peer_outcomes)
    # 200-char cap.
    for p in ctx.peer_outcomes:
        assert len(p.summary) <= 200


# ---------------------------------------------------------------------------
# 4. own_guard_events filtered by role
# ---------------------------------------------------------------------------


def test_dialogue_context_filters_guard_events_by_role(tmp_path):
    orch = _orchestrator(tmp_path)
    comment = ReviewComment(file="src/x.py", line=42, severity="blocker", body="b")
    round1 = [
        _outcome(role=BotRole.ARCHITECT, verdict=ReviewVerdict.REQUEST_CHANGES, comments=[comment])
    ]
    events = [
        GuardEvent(
            role=BotRole.ARCHITECT,
            file="src/x.py",
            line=42,
            original_severity="blocker",
            reason="missing_token_found_in_file",
            tokens_found=("Args:",),
            original_body="b",
        ),
        GuardEvent(
            role=BotRole.SCRIBE,
            file="src/y.py",
            line=10,
            original_severity="major",
            reason="self_referential",
            original_body="other",
        ),
    ]
    ctx = orch._build_dialogue_context(
        reviewer_role=BotRole.ARCHITECT,
        reviewer_outcome=round1[0],
        round1_outcomes=round1,
        guard_events=events,
    )
    assert len(ctx.own_guard_events) == 1
    assert ctx.own_guard_events[0].reason == "missing_token_found_in_file"
    assert ctx.own_guard_events[0].comment_index == 0


# ---------------------------------------------------------------------------
# 5. file excerpts around cited lines
# ---------------------------------------------------------------------------


def test_make_file_excerpts_reads_window_around_cited_line(tmp_path):
    target = tmp_path / "src" / "x.py"
    target.parent.mkdir(parents=True)
    body = "\n".join(f"line {n}" for n in range(1, 51))
    target.write_text(body, encoding="utf-8")
    comments = [ReviewComment(file="src/x.py", line=20, severity="blocker", body="b")]
    excerpts = make_file_excerpts(comments, repo_root=tmp_path, window=5)
    assert len(excerpts) == 1
    ex = excerpts[0]
    assert ex.center_line == 20
    assert ex.start_line == 15
    # 30-line window: 15..15+15+15-1 → here 15..24 inclusive (window=5: 5 above + cited + 4 below).
    assert "line 20" in ex.text
    assert "line 15" in ex.text


def test_make_file_excerpts_skips_missing_files(tmp_path):
    comments = [ReviewComment(file="nope.py", line=1, severity="blocker", body="b")]
    excerpts = make_file_excerpts(comments, repo_root=tmp_path)
    assert excerpts == ()


def test_make_file_excerpts_dedupes(tmp_path):
    target = tmp_path / "src" / "x.py"
    target.parent.mkdir(parents=True)
    target.write_text("line\n" * 50, encoding="utf-8")
    comments = [
        ReviewComment(file="src/x.py", line=10, severity="blocker", body="b1"),
        ReviewComment(file="src/x.py", line=10, severity="major", body="b2"),
    ]
    excerpts = make_file_excerpts(comments, repo_root=tmp_path)
    assert len(excerpts) == 1


# ---------------------------------------------------------------------------
# 6. retracted comments drop + verdict softens
# ---------------------------------------------------------------------------


def test_drop_retracted_comments_removes_retracted():
    out = _outcome(
        role=BotRole.SCRIBE,
        verdict=ReviewVerdict.REQUEST_CHANGES,
        comments=[
            ReviewComment(file="x.py", line=1, severity="retracted", body="b"),
            ReviewComment(file="y.py", line=1, severity="major", body="real"),
        ],
    )
    new = _drop_retracted_comments(out)
    assert len(new.comments) == 1
    assert new.comments[0].severity == "major"


def test_drop_retracted_softens_verdict_when_all_blockers_retract():
    out = _outcome(
        role=BotRole.SCRIBE,
        verdict=ReviewVerdict.REQUEST_CHANGES,
        comments=[
            ReviewComment(file="x.py", line=1, severity="retracted", body="was-blocker"),
            ReviewComment(file="y.py", line=1, severity="nit", body="real-nit"),
        ],
    )
    new = _drop_retracted_comments(out)
    assert new.verdict is ReviewVerdict.COMMENT
    assert len(new.comments) == 1
    assert new.comments[0].severity == "nit"


def test_drop_retracted_keeps_verdict_when_blocker_remains():
    out = _outcome(
        role=BotRole.ARCHITECT,
        verdict=ReviewVerdict.REQUEST_CHANGES,
        comments=[
            ReviewComment(file="x.py", line=1, severity="retracted", body="r"),
            ReviewComment(file="y.py", line=1, severity="blocker", body="real-blocker"),
        ],
    )
    new = _drop_retracted_comments(out)
    assert new.verdict is ReviewVerdict.REQUEST_CHANGES


# ---------------------------------------------------------------------------
# 7. parser accepts severity="retracted"
# ---------------------------------------------------------------------------


def test_reviewer_parser_accepts_retracted_severity():
    arch = Architect(loop=MagicMock())
    raw = (
        '{"verdict": "COMMENT", "summary": "round 2 retraction",'
        ' "comments": [{"file": "x.py", "line": 1,'
        ' "severity": "retracted", "body": "withdrawn"}]}'
    )
    _verdict, _summary, comments = arch._parse_response(raw)
    assert comments[0].severity == "retracted"


# ---------------------------------------------------------------------------
# 8. dataclass shape
# ---------------------------------------------------------------------------


def test_dialogue_context_is_frozen_with_tuple_fields():
    ctx = DialogueContext()
    with pytest.raises((AttributeError, TypeError)):
        ctx.peer_outcomes = (PeerOutcome(BotRole.ARCHITECT, ReviewVerdict.APPROVE, "x"),)
    assert isinstance(ctx.peer_outcomes, tuple)
    assert isinstance(ctx.own_guard_events, tuple)
    assert isinstance(ctx.file_excerpts, tuple)


# ---------------------------------------------------------------------------
# 9. _locate_comment_index
# ---------------------------------------------------------------------------


def test_locate_comment_index_matches_by_file_line():
    comments = [
        ReviewComment(file="a.py", line=1, severity="blocker", body="x"),
        ReviewComment(file="b.py", line=2, severity="major", body="y"),
    ]
    ev = GuardEvent(
        role=BotRole.ARCHITECT,
        file="b.py",
        line=2,
        original_severity="major",
        reason="r",
    )
    assert _locate_comment_index(comments, ev) == 1


def test_locate_comment_index_returns_minus1_on_no_match():
    comments = [ReviewComment(file="a.py", line=1, severity="blocker", body="x")]
    ev = GuardEvent(
        role=BotRole.ARCHITECT,
        file="other.py",
        line=999,
        original_severity="blocker",
        reason="r",
    )
    assert _locate_comment_index(comments, ev) == -1


# ---------------------------------------------------------------------------
# 10. dialogue_context placeholder substitution end-to-end
# ---------------------------------------------------------------------------


def test_render_prompt_substitutes_dialogue_placeholder(tmp_path, monkeypatch):
    arch = Architect(loop=MagicMock())
    ctx = DialogueContext(
        peer_outcomes=(PeerOutcome(BotRole.SCRIBE, ReviewVerdict.APPROVE, "looks good"),),
        own_guard_events=(GuardEventSummary(0, "missing-X verified present"),),
        file_excerpts=(FileExcerpt(file="x.py", center_line=5, start_line=1, text="    1  abc"),),
    )
    rendered = arch._render_prompt("DIFF_CONTENT", spec_plan=None, dialogue_context=ctx)
    assert "{DIALOGUE_CONTEXT}" not in rendered
    assert "looks good" in rendered
    assert "missing-X verified present" in rendered
    assert "x.py" in rendered


def test_render_prompt_falls_back_when_no_dialogue_context():
    arch = Architect(loop=MagicMock())
    rendered = arch._render_prompt("DIFF", spec_plan=None, dialogue_context=None)
    assert "{DIALOGUE_CONTEXT}" not in rendered
    assert "round 1" in rendered
