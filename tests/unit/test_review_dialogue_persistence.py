"""Tests for the SP-REVIEW-DIALOGUE-C persistence layer.

Exercises :func:`ferova.review.persistence.record_dialogue` /
:func:`fetch_dialogue` end-to-end, plus the orchestrator wire-in
(round-1 outcomes recorded, round-2 only for re-running reviewers,
Coder challenges recorded under ``round="challenge"``) and the
``## Dialogue transcript`` rendered in the archive comment.

No network and no real ``gh``: the orchestrator's ``_StubGhCli``
captures the body that would have been pushed and tests inspect it
directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ferova.review.gh_client import GhResult
from ferova.review.orchestrator import (
    ReviewTeamOrchestrator,
    _render_dialogue_transcript,
)
from ferova.review.persistence import (
    DialogueEntry,
    fetch_dialogue,
    init_schema,
    record_dialogue,
)
from ferova.review.reviewer import (
    BotRole,
    ReviewComment,
    ReviewerOutcome,
    ReviewVerdict,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _outcome(
    role: BotRole,
    verdict: ReviewVerdict,
    *,
    comments: list[ReviewComment] | None = None,
) -> ReviewerOutcome:
    return ReviewerOutcome(
        role=role,
        verdict=verdict,
        summary=f"{role.value} says {verdict.value}",
        comments=comments or [],
        model_used="model",
        elapsed_s=0.1,
        tokens_used=10,
        raw_response="",
    )


class _StubReviewer:
    """Reviewer stub that returns successive canned outcomes."""

    def __init__(self, outcomes: list[ReviewerOutcome]) -> None:
        self.role = outcomes[0].role
        self._outcomes = list(outcomes)
        self.calls = 0

    def review_diff(self, diff: str, **kwargs: Any) -> ReviewerOutcome:
        idx = min(self.calls, len(self._outcomes) - 1)
        self.calls += 1
        return self._outcomes[idx]


class _StubGhCli:
    """Capture archive bodies; returns canned diff."""

    ARCHIVE_MARKER = "<!-- ferova-review-archive -->"

    def __init__(self) -> None:
        self.archive_calls: list[dict] = []
        self.posted_reviews: list[dict] = []
        self.posted_comments: list[dict] = []

    def pr_diff(self, pr_number: int) -> GhResult:
        return GhResult(0, "diff --git a/x b/x\n", "", argv=["gh"])

    def pr_head_sha(self, pr_number: int) -> str:
        return "deadbeef"

    def pr_view(self, pr_number: int) -> dict | None:
        return None

    def pr_review_comment(self, pr_number: int, **kw: Any) -> GhResult:
        self.posted_comments.append({"pr": pr_number, **kw})
        return GhResult(0, "", "", argv=["gh"])

    def pr_review_submit(self, pr_number: int, **kw: Any) -> GhResult:
        self.posted_reviews.append({"pr": pr_number, **kw})
        return GhResult(0, "", "", argv=["gh"])

    def upsert_archive_comment(self, pr_number: int, *, body: str) -> GhResult:
        self.archive_calls.append({"pr": pr_number, "body": body})
        return GhResult(0, "", "", argv=["gh"])

    def fetch_archive_comment(self, pr_number: int) -> str | None:
        return None


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "review.db"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_record_dialogue_creates_table_on_first_call(db_path: Path) -> None:
    """First call to record_dialogue creates the table — no manual init needed."""
    record_dialogue(
        db_path,
        pr_number=1,
        round="1",
        speaker="architect",
        payload={"verdict": "APPROVE"},
    )
    entries = fetch_dialogue(1, db_path=db_path)
    assert len(entries) == 1
    assert entries[0].speaker == "architect"
    assert entries[0].round == "1"


def test_record_dialogue_persists_round_1_outcomes(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All four round-1 reviewers land in pr_review_dialogue with round='1'."""
    outs = {
        BotRole.ARCHITECT: _outcome(BotRole.ARCHITECT, ReviewVerdict.APPROVE),
        BotRole.SENTINEL: _outcome(BotRole.SENTINEL, ReviewVerdict.APPROVE),
        BotRole.TESTER: _outcome(BotRole.TESTER, ReviewVerdict.APPROVE),
        BotRole.SCRIBE: _outcome(BotRole.SCRIBE, ReviewVerdict.APPROVE),
    }
    cls_to_role = {
        "Architect": BotRole.ARCHITECT,
        "Sentinel": BotRole.SENTINEL,
        "Tester": BotRole.TESTER,
        "Scribe": BotRole.SCRIBE,
    }
    for cls, role in cls_to_role.items():
        monkeypatch.setattr(
            f"ferova.review.orchestrator.{cls}",
            lambda r=role, **_kw: _StubReviewer([outs[r]]),
        )

    gh = _StubGhCli()
    orch = ReviewTeamOrchestrator(gh=gh, db_path=db_path, post_to_github=False)
    orch.review_pr(pr_number=42)

    entries = fetch_dialogue(42, db_path=db_path)
    speakers = {e.speaker for e in entries if e.round == "1"}
    assert speakers == {"architect", "sentinel", "tester", "scribe"}


def test_record_dialogue_persists_round_2_only_for_rerun_reviewers(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only reviewers whose outcome was replaced get a round='2' row.

    Architect flags a blocker → triggers round 2.  Other three go in
    APPROVE and never re-run.  Result: one round-2 row (architect).
    """
    arch_blocker = _outcome(
        BotRole.ARCHITECT,
        ReviewVerdict.REQUEST_CHANGES,
        comments=[ReviewComment(file="a.py", line=1, severity="blocker", body="bad")],
    )
    arch_round2 = _outcome(BotRole.ARCHITECT, ReviewVerdict.APPROVE)
    monkeypatch.setattr(
        "ferova.review.orchestrator.Architect",
        lambda **_kw: _StubReviewer([arch_blocker, arch_round2]),
    )
    for cls, role in [
        ("Sentinel", BotRole.SENTINEL),
        ("Tester", BotRole.TESTER),
        ("Scribe", BotRole.SCRIBE),
    ]:
        monkeypatch.setattr(
            f"ferova.review.orchestrator.{cls}",
            lambda r=role, **_kw: _StubReviewer([_outcome(r, ReviewVerdict.APPROVE)]),
        )

    gh = _StubGhCli()
    orch = ReviewTeamOrchestrator(gh=gh, db_path=db_path, post_to_github=False)
    orch.review_pr(pr_number=43)

    entries = fetch_dialogue(43, db_path=db_path)
    round2 = [e for e in entries if e.round == "2"]
    assert len(round2) == 1
    assert round2[0].speaker == "architect"


def test_record_dialogue_persists_coder_challenges(db_path: Path) -> None:
    """Coder challenge records land with round='challenge', speaker='coder'."""
    record_dialogue(
        db_path,
        pr_number=44,
        round="challenge",
        speaker="coder",
        payload={
            "comment_index": 0,
            "decision": "CHALLENGE",
            "rationale": "Args at line 343",
            "evidence_path": "src/foo.py",
            "evidence_lines": "343-358",
        },
    )
    entries = fetch_dialogue(44, db_path=db_path)
    assert len(entries) == 1
    assert entries[0].round == "challenge"
    assert entries[0].speaker == "coder"
    assert entries[0].payload["decision"] == "CHALLENGE"
    assert entries[0].payload["evidence_path"] == "src/foo.py"


def test_fetch_dialogue_returns_entries_in_chronological_order(
    db_path: Path,
) -> None:
    """Entries come out in insertion order regardless of round value."""
    init_schema(db_path)
    record_dialogue(db_path, pr_number=1, round="1", speaker="architect", payload={"i": 0})
    record_dialogue(db_path, pr_number=1, round="2", speaker="sentinel", payload={"i": 1})
    record_dialogue(db_path, pr_number=1, round="challenge", speaker="coder", payload={"i": 2})
    entries = fetch_dialogue(1, db_path=db_path)
    assert [e.payload["i"] for e in entries] == [0, 1, 2]


def test_archive_comment_renders_dialogue_section_when_db_has_rows(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orchestrator's archive body carries `## Dialogue transcript`."""
    for cls, role in [
        ("Architect", BotRole.ARCHITECT),
        ("Sentinel", BotRole.SENTINEL),
        ("Tester", BotRole.TESTER),
        ("Scribe", BotRole.SCRIBE),
    ]:
        monkeypatch.setattr(
            f"ferova.review.orchestrator.{cls}",
            lambda r=role, **_kw: _StubReviewer([_outcome(r, ReviewVerdict.APPROVE)]),
        )
    gh = _StubGhCli()
    orch = ReviewTeamOrchestrator(gh=gh, db_path=db_path, post_to_github=True)
    orch.review_pr(pr_number=45)
    assert gh.archive_calls, "archive comment should be upserted"
    body = gh.archive_calls[-1]["body"]
    assert "## Dialogue transcript" in body
    assert "### Round 1" in body
    assert "Architect" in body


def test_render_dialogue_transcript_groups_by_round() -> None:
    """The renderer puts round-1 / round-2 / challenge entries in order."""
    from datetime import UTC
    from datetime import datetime as _dt

    now = _dt.now(UTC)
    entries = [
        DialogueEntry(
            pr_number=1,
            round="1",
            speaker="architect",
            payload={"verdict": "APPROVE", "summary": "looks fine"},
            created_at=now,
        ),
        DialogueEntry(
            pr_number=1,
            round="challenge",
            speaker="coder",
            payload={
                "comment_index": 0,
                "decision": "CHALLENGE",
                "rationale": "Args at 343",
                "evidence_path": "src/foo.py",
                "evidence_lines": "343-358",
            },
            created_at=now,
        ),
    ]
    text = _render_dialogue_transcript(entries)
    assert text.startswith("## Dialogue transcript")
    assert "### Round 1" in text
    assert "### Coder challenges" in text
    assert "**CHALLENGE**" in text
    assert "src/foo.py:343-358" in text


def test_render_dialogue_transcript_empty_when_no_entries() -> None:
    """Renderer returns an empty string for an empty list."""
    assert _render_dialogue_transcript([]) == ""
