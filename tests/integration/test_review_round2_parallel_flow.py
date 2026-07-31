"""Integration test: pooled round 2 composes with round-1 pooling end to end.

Exercises ``ReviewTeamOrchestrator.review_pr`` (SP-REVIEW-ROUND2-PARALLEL) against a
real scratch git repository under ``tmp_path``, a truthful ``GhCli`` fake, and REAL
``Architect`` / ``Sentinel`` / ``Tester`` / ``Scribe`` instances whose only replaced
surface is the public ``review_diff`` contract method. Two reviewers' round-1
outcomes flag a blocker, triggering round 2 for both; their round-2 ``review_diff``
calls rendezvous on a shared ``threading.Barrier(parties=2, timeout=10)``, which only
releases if the pooled round-2 pass runs them concurrently rather than one at a time
in the old sequential loop — proving the fix composes with the rest of the pipeline
(outcome assembly, ledger writes stubbed) and not just the isolated ``_round_two``
method.
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from repoach.review.merge_gate import MergeFacts
from repoach.review.orchestrator import ReviewTeamOrchestrator
from repoach.review.reviewer import (
    Architect,
    BotRole,
    DialogueContext,
    ReviewComment,
    Reviewer,
    ReviewerOutcome,
    ReviewVerdict,
    Scribe,
    Sentinel,
    Tester,
)

_ROLE_TO_CLASS: dict[BotRole, type[Reviewer]] = {
    BotRole.ARCHITECT: Architect,
    BotRole.SENTINEL: Sentinel,
    BotRole.TESTER: Tester,
    BotRole.SCRIBE: Scribe,
}


def _canned(role: BotRole, verdict: ReviewVerdict = ReviewVerdict.APPROVE) -> ReviewerOutcome:
    return ReviewerOutcome(
        role=role,
        verdict=verdict,
        summary="canned",
        comments=[],
        model_used="test-model",
        elapsed_s=0.01,
        tokens_used=1,
        raw_response="{}",
    )


def _blocker_outcome(role: BotRole) -> ReviewerOutcome:
    return ReviewerOutcome(
        role=role,
        verdict=ReviewVerdict.REQUEST_CHANGES,
        summary="round1 blocker",
        comments=[ReviewComment(file="a.py", line=1, severity="blocker", body="fix this")],
        model_used="test-model",
        elapsed_s=0.01,
        tokens_used=1,
        raw_response="{}",
    )


def _boundary_fake_reviewer(
    role: BotRole,
    review_diff: Callable[..., ReviewerOutcome],
) -> Reviewer:
    """A real Architect/Sentinel/Tester/Scribe with only review_diff replaced."""
    instance = _ROLE_TO_CLASS[role](loop=MagicMock())
    instance.review_diff = review_diff
    return instance


class _GhCliFake:
    def __init__(self, *, head_sha: str, head_ref: str = "feat/x") -> None:
        self.head_sha = head_sha
        self.head_ref = head_ref

    def pr_diff(self, pr_number: int) -> object:
        result = MagicMock()
        result.ok = True
        result.stdout = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n+x = 1\n"
        result.stderr = ""
        return result

    def pr_view(self, pr_number: int) -> dict[str, object]:
        return {"headRefName": self.head_ref, "title": "test"}

    def pr_head_sha(self, pr_number: int) -> str | None:
        return self.head_sha

    def list_review_comments(self, pr_number: int) -> list[dict[str, object]]:
        return []

    def pr_review_comment(self, pr_number: int, **kw: object) -> object:
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        return r

    def pr_review_submit(self, pr_number: int, **kw: object) -> object:
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        return r

    def upsert_archive_comment(self, pr_number: int, *, body: str) -> object:
        r = MagicMock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        return r

    def fetch_archive_comment(self, pr_number: int) -> str | None:
        return None


def _silence_downstream_ledger_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise real-DB side effects past the fan-out, matching SP-FRESH-HEAD-CONCURRENT."""
    monkeypatch.setattr("repoach.review.orchestrator.fetch_dialogue", lambda *a, **kw: [])
    monkeypatch.setattr("repoach.review.orchestrator.record_dialogue", lambda *a, **kw: None)
    monkeypatch.setattr("repoach.review.orchestrator.record_review_ledger", lambda *a, **kw: True)
    monkeypatch.setattr("repoach.review.orchestrator.record_review", lambda *a, **kw: None)
    monkeypatch.setattr("repoach.review.orchestrator.record_hallucination", lambda *a, **kw: None)
    monkeypatch.setattr("repoach.review.orchestrator.verify_findings_for_pr", lambda *a, **kw: {})
    monkeypatch.setattr("repoach.review.orchestrator.judge_findings_for_pr", lambda *a, **kw: {})
    monkeypatch.setattr(
        "repoach.review.orchestrator.remember_verified_findings", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        "repoach.review.orchestrator._build_review_lessons_block",
        lambda pr_title, diff: "",
    )
    monkeypatch.setattr(
        "repoach.review.orchestrator.summarise_ledger_facts",
        lambda *a, **kw: MergeFacts(
            head_sha="x",
            ci_green=True,
            open_blocking_findings=0,
            spec_covered=False,
            spec_coverage_known=False,
            review_complete=True,
            review_integrity_known=False,
            review_integrity_any=False,
        ),
    )
    monkeypatch.setattr(
        "repoach.review.thread_context.post_refuted_finding_sentinels",
        lambda *a, **kw: 0,
    )


def test_round2_barrier_gated_reviewers_complete_through_full_review_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-1 blockers trigger round 2 for 2 reviewers; their calls rendezvous on a Barrier.

    Under the pre-change sequential round-2 loop, the second triggered reviewer's
    ``review_diff`` is only invoked after the first one has already returned, so the
    barrier can never release within its 10s timeout. Pooled round 2 submits both
    before waiting on either result, so both reach the barrier together and the
    flow completes with the expected final outcomes.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "feat/x", str(repo_root)], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "s",
        ],
        cwd=repo_root,
        check=True,
    )

    gh = _GhCliFake(head_sha="cafe" * 8, head_ref="feat/x")

    barrier = threading.Barrier(parties=2, timeout=10)

    def _round2_gated_review_diff(
        role: BotRole, round1_outcome: ReviewerOutcome
    ) -> Callable[..., ReviewerOutcome]:
        def _review_diff(
            diff: str, *, dialogue_context: DialogueContext | None = None, **kwargs: object
        ) -> ReviewerOutcome:
            if dialogue_context is None:
                return round1_outcome
            barrier.wait()
            return _canned(role, ReviewVerdict.APPROVE)

        return _review_diff

    round1_outcomes = {
        BotRole.ARCHITECT: _blocker_outcome(BotRole.ARCHITECT),
        BotRole.SENTINEL: _blocker_outcome(BotRole.SENTINEL),
        BotRole.TESTER: _canned(BotRole.TESTER),
        BotRole.SCRIBE: _canned(BotRole.SCRIBE),
    }

    for role, cls_name in (
        (BotRole.ARCHITECT, "Architect"),
        (BotRole.SENTINEL, "Sentinel"),
        (BotRole.TESTER, "Tester"),
        (BotRole.SCRIBE, "Scribe"),
    ):
        review_diff = _round2_gated_review_diff(role, round1_outcomes[role])
        monkeypatch.setattr(
            f"repoach.review.orchestrator.{cls_name}",
            lambda db_path=None, role=role, review_diff=review_diff: _boundary_fake_reviewer(
                role, review_diff
            ),
        )

    from repoach.review.orchestrator import resolve_fresh_head

    monkeypatch.setitem(resolve_fresh_head.__kwdefaults__, "delay_s", 0.2)
    _silence_downstream_ledger_writes(monkeypatch)

    orch = ReviewTeamOrchestrator(
        gh=gh,
        db_path=tmp_path / "review.db",
        post_to_github=True,
        max_workers=4,
        repo_root=repo_root,
    )

    monkeypatch.setattr(orch, "_fire_routine", lambda _team: None)
    monkeypatch.setattr(orch, "_fire_proposed_escalation_dossier", lambda _pn: None)
    monkeypatch.setattr(orch, "_upsert_archive_comment", lambda _team: None)
    monkeypatch.setattr(orch, "_run_auto_challenge_pass", lambda **kw: None)
    monkeypatch.setattr(orch, "_publish_outcome", lambda **kw: None)

    team = orch.review_pr(pr_number=7)

    assert team.head_sha == "cafe" * 8
    outcomes_by_role = {o.role: o for o in team.reviews}
    assert outcomes_by_role[BotRole.ARCHITECT].verdict is ReviewVerdict.APPROVE
    assert outcomes_by_role[BotRole.SENTINEL].verdict is ReviewVerdict.APPROVE
    assert outcomes_by_role[BotRole.TESTER].verdict is ReviewVerdict.APPROVE
    assert outcomes_by_role[BotRole.SCRIBE].verdict is ReviewVerdict.APPROVE
