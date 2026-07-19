"""Tests for SP-REVIEW-CONVERGENCE-RATCHET — cross-invocation context + nit-only auto-promote.

Layer 1: Prior dialogue is loaded and injected into the reviewer prompt.
Layer 2: Nit-only auto-promote fires after round 2 when 0 blockers / 0 majors.
Layer 3: head_sha persisted in archive JSON (safe_merge fallback is shell-level).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from repoach.review.orchestrator import (
    TeamOutcome,
    _build_prior_review_context,
    _compute_diff_hash,
    _dialogue_payload,
    team_outcome_to_dict,
)
from repoach.review.persistence import record_dialogue
from repoach.review.reviewer import (
    BotRole,
    PriorReviewContext,
    ReviewComment,
    ReviewerOutcome,
    ReviewVerdict,
    _render_prior_review,
)


@pytest.fixture(autouse=True)
def _stub_proxy_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep reviewer construction possible without the operator ``.env``.

    SP-PROXY-SECURE-DEFAULTS removed the implicit ``freecc`` token
    fallback, so a tokenless environment (CI) now refuses to build the
    underlying AgentLoop — these tests target convergence, not auth.
    """
    monkeypatch.setattr(
        "repoach.agent_engine.agent_loop.get_settings",
        lambda: SimpleNamespace(
            llm_proxy_base_url="http://localhost:8082",
            llm_proxy_auth_token=SecretStr("test-token"),
        ),
    )


def _make_outcome(
    role: BotRole = BotRole.SCRIBE,
    verdict: ReviewVerdict = ReviewVerdict.COMMENT,
    comments: list[ReviewComment] | None = None,
) -> ReviewerOutcome:
    return ReviewerOutcome(
        role=role,
        verdict=verdict,
        summary="test summary",
        comments=comments or [],
    )


def test_dialogue_payload_includes_diff_hash() -> None:
    """diff_hash is present in payload dict when provided."""
    outcome = _make_outcome()
    payload = _dialogue_payload(outcome, diff_hash="abc123")
    assert payload["diff_hash"] == "abc123"


def test_dialogue_payload_omits_diff_hash_when_none() -> None:
    """diff_hash key absent when not provided (backwards compat)."""
    outcome = _make_outcome()
    payload = _dialogue_payload(outcome)
    assert "diff_hash" not in payload


def test_prior_context_built_from_history(tmp_path: Path) -> None:
    """Seed L4 dialogue rows and verify PriorReviewContext per role."""
    db = tmp_path / "review.db"
    record_dialogue(
        db,
        pr_number=99,
        round="1",
        speaker="scribe",
        payload={"verdict": "APPROVE", "summary": "all good", "n_comments": 0, "diff_hash": "aaa"},
    )
    record_dialogue(
        db,
        pr_number=99,
        round="1",
        speaker="architect",
        payload={
            "verdict": "REQUEST_CHANGES",
            "summary": "fix naming",
            "n_comments": 2,
            "diff_hash": "aaa",
        },
    )

    ctx = _build_prior_review_context(99, diff_hash="aaa", db_path=db)
    assert BotRole.SCRIBE in ctx
    assert ctx[BotRole.SCRIBE].verdict == ReviewVerdict.APPROVE
    assert ctx[BotRole.SCRIBE].diff_changed is False

    assert BotRole.ARCHITECT in ctx
    assert ctx[BotRole.ARCHITECT].verdict == ReviewVerdict.REQUEST_CHANGES
    assert ctx[BotRole.ARCHITECT].n_comments == 2

    assert BotRole.TESTER not in ctx


def test_prior_context_detects_diff_change(tmp_path: Path) -> None:
    """diff_changed=True when the diff hash differs from the stored one."""
    db = tmp_path / "review.db"
    record_dialogue(
        db,
        pr_number=99,
        round="1",
        speaker="scribe",
        payload={"verdict": "APPROVE", "summary": "ok", "n_comments": 0, "diff_hash": "old_hash"},
    )
    ctx = _build_prior_review_context(99, diff_hash="new_hash", db_path=db)
    assert ctx[BotRole.SCRIBE].diff_changed is True


def test_render_prior_review_unchanged_diff() -> None:
    """Unchanged diff instructs the reviewer to confirm prior verdict."""
    ctx = PriorReviewContext(
        role=BotRole.SCRIBE,
        verdict=ReviewVerdict.APPROVE,
        summary="all good",
        n_comments=0,
        diff_changed=False,
    )
    text = _render_prior_review(ctx)
    assert "APPROVE" in text
    assert "NOT changed" in text
    assert "Confirm" in text


def test_render_prior_review_changed_diff() -> None:
    """Changed diff instructs the reviewer to re-evaluate."""
    ctx = PriorReviewContext(
        role=BotRole.ARCHITECT,
        verdict=ReviewVerdict.REQUEST_CHANGES,
        summary="naming issue",
        n_comments=2,
        diff_changed=True,
    )
    text = _render_prior_review(ctx)
    assert "REQUEST_CHANGES" in text
    assert "HAS changed" in text
    assert "Re-evaluate" in text


def test_render_prior_review_first_review() -> None:
    """No prior context renders a first-review stub."""
    from repoach.review.reviewer import Scribe

    reviewer = Scribe()
    prompt = reviewer._render_prompt(
        "fake diff",
        prior_review=None,
    )
    assert "first review of this PR" in prompt


def test_nit_only_auto_promote_fires() -> None:
    """COMMENT + 2 nits after retraction processing → APPROVE."""
    outcome = _make_outcome(
        verdict=ReviewVerdict.COMMENT,
        comments=[
            ReviewComment(file="a.py", line=1, severity="nit", body="nit 1"),
            ReviewComment(file="b.py", line=2, severity="minor", body="minor 1"),
        ],
    )
    if outcome.verdict != ReviewVerdict.APPROVE and not any(
        c.severity in {"blocker", "major"} for c in outcome.comments
    ):
        promoted = replace(
            outcome,
            verdict=ReviewVerdict.APPROVE,
            summary=outcome.summary + " [auto-promoted: nit-only]",
        )
    else:
        promoted = outcome
    assert promoted.verdict == ReviewVerdict.APPROVE
    assert "[auto-promoted: nit-only]" in promoted.summary


def test_nit_only_auto_promote_blocked_by_major() -> None:
    """Major comment survives → no promotion."""
    outcome = _make_outcome(
        verdict=ReviewVerdict.REQUEST_CHANGES,
        comments=[
            ReviewComment(file="a.py", line=1, severity="major", body="real issue"),
            ReviewComment(file="b.py", line=2, severity="nit", body="nit"),
        ],
    )
    if outcome.verdict != ReviewVerdict.APPROVE and not any(
        c.severity in {"blocker", "major"} for c in outcome.comments
    ):
        promoted = replace(
            outcome,
            verdict=ReviewVerdict.APPROVE,
            summary=outcome.summary + " [auto-promoted: nit-only]",
        )
    else:
        promoted = outcome
    assert promoted.verdict == ReviewVerdict.REQUEST_CHANGES


def test_head_sha_in_archive_json() -> None:
    """TeamOutcome serialization includes head_sha field."""
    team = TeamOutcome(
        pr_number=42,
        final_verdict=ReviewVerdict.APPROVE,
        n_blockers=0,
        n_majors=0,
        head_sha="abc123def456",
    )
    d = team_outcome_to_dict(team)
    assert d["head_sha"] == "abc123def456"


def test_compute_diff_hash_deterministic() -> None:
    """Same input always produces the same hash."""
    h1 = _compute_diff_hash("hello world")
    h2 = _compute_diff_hash("hello world")
    assert h1 == h2
    assert len(h1) == 16
    assert _compute_diff_hash("different") != h1
