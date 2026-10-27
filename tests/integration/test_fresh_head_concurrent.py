"""Integration test: the fresh-head guard runs concurrently with the reviewer fan-out.

Exercises ``ReviewTeamOrchestrator.review_pr`` end-to-end (SP-FRESH-HEAD-CONCURRENT)
against a real scratch git repository under ``tmp_path``, a truthful ``gh`` fake,
and REAL ``Architect`` / ``Sentinel`` / ``Tester`` / ``Scribe`` instances whose only
replaced surface is the public ``review_diff`` contract method — never an ad-hoc stub
class with an invented signature. Asserts ``head_sha`` resolves correctly and that the
guard's call interval overlapped the reviewer fan-out instead of preceding it.

Overlap is proven structurally by :mod:`tests.overlap_fakes`, never by a wall
clock.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from repoach.forge.github import GitHubForge
from repoach.forge.runner import CommandResult
from repoach.review.finding_verifiers import VerificationReport
from repoach.review.head_reverify import HeadReverification
from repoach.review.merge_gate import MergeFacts
from repoach.review.orchestrator import ReviewTeamOrchestrator
from repoach.review.refuter import JudgingReport
from repoach.review.reviewer import (
    Architect,
    BotRole,
    Reviewer,
    ReviewerOutcome,
    ReviewVerdict,
    Scribe,
    Sentinel,
    Tester,
)
from tests.forge_fakes import gh_ok, pr_view_stdout, scripted_github
from tests.overlap_fakes import (
    ConcurrencyTracker,
    assert_guard_overlapped_fanout,
    make_tracked_guard,
    make_tracked_review_diff,
)

_ROLE_TO_CLASS: dict[BotRole, type[Reviewer]] = {
    BotRole.ARCHITECT: Architect,
    BotRole.SENTINEL: Sentinel,
    BotRole.TESTER: Tester,
    BotRole.SCRIBE: Scribe,
}


def _canned(role: BotRole) -> ReviewerOutcome:
    """Return a minimal APPROVE outcome for a single role."""
    return ReviewerOutcome(
        role=role,
        verdict=ReviewVerdict.APPROVE,
        summary="canned",
        comments=[],
        model_used="test-model",
        elapsed_s=0.01,
        tokens_used=1,
        raw_response="{}",
    )


def _boundary_fake_reviewer(
    role: BotRole,
    review_diff: Callable[..., ReviewerOutcome],
) -> Reviewer:
    """A real Architect/Sentinel/Tester/Scribe with only review_diff replaced.

    The production reviewer class is instantiated for real (its ``AgentLoop``
    boundary faked via a ``MagicMock`` that is never exercised), keeping ``.role``
    and the rest of the object's surface truthful. Only the public ``review_diff``
    contract — the boundary the orchestrator's fan-out actually calls across — is
    substituted.
    """
    instance = _ROLE_TO_CLASS[role](loop=MagicMock())
    instance.review_diff = review_diff
    return instance


def _gh_fake(*, head_sha: str = "stale" * 8, head_ref: str = "feat/x") -> GitHubForge:
    """A real :class:`GitHubForge` whose ``gh`` surfaces a stale-forever head SHA.

    The scratch git repo has its own HEAD; ``gh pr view`` always reports a
    different SHA (``\"stale\" * 8``) while naming the branch correctly,
    which is exactly the condition that makes ``resolve_fresh_head``
    re-poll until it exhausts its attempts and gives up loudly.
    """

    def _run(args: list[str]) -> CommandResult:
        if args[:2] == ["pr", "diff"]:
            return gh_ok(
                "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n+x = 1\n",
                argv=args,
            )
        if args[:2] == ["pr", "view"]:
            return gh_ok(
                pr_view_stdout(3, head_ref=head_ref, head_sha=head_sha, title="test"), argv=args
            )
        if args[:3] == ["api", "--paginate", "--slurp"]:
            return gh_ok("[[]]", argv=args)
        return gh_ok("{}", argv=args)

    return scripted_github(run=_run)


def _silence_downstream_ledger_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise every real-DB side effect review_pr performs past the fan-out.

    None of these functions are the subject under test — SP-FRESH-HEAD-CONCURRENT is
    a scheduling change to the call site, not to findings persistence — so replacing
    them keeps the test about scheduling instead of entangling it with incidental
    SQLite commit latency.
    """
    monkeypatch.setattr("repoach.review.orchestrator.fetch_dialogue", lambda *a, **kw: [])
    monkeypatch.setattr("repoach.review.orchestrator.record_dialogue", lambda *a, **kw: None)
    monkeypatch.setattr("repoach.review.orchestrator.record_review_ledger", lambda *a, **kw: None)
    monkeypatch.setattr("repoach.review.orchestrator.record_review", lambda *a, **kw: None)
    monkeypatch.setattr("repoach.review.orchestrator.record_hallucination", lambda *a, **kw: None)
    monkeypatch.setattr(
        "repoach.review.orchestrator.verify_findings_for_pr", lambda *a, **kw: VerificationReport()
    )
    monkeypatch.setattr(
        "repoach.review.orchestrator.judge_findings_for_pr", lambda *a, **kw: JudgingReport()
    )
    monkeypatch.setattr(
        "repoach.review.orchestrator.skipped_reverification",
        lambda *a, head_sha, **kw: HeadReverification(head_sha=head_sha),
    )
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


def test_concurrent_head_guard_integration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The head guard resolves correctly and overlaps the reviewer fan-out end to end.

    Constructs an orchestrator with a real scratch git repo under ``tmp_path``, a
    truthful stale-forever ``_gh_fake``, the real, unmodified ``resolve_fresh_head``
    wrapped by a tracker whose injected ``sleep`` waits for the first reviewer, and
    boundary-fake reviewers that record their own interval. Asserts ``head_sha`` is
    the stale SHA the fake returns after the guard exhausts its retries, that a
    reviewer entered while the guard was still inside its call (maximum
    concurrency of at least two, and intersecting intervals), and only a generous
    sanity bound on wall time.
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

    gh = _gh_fake(head_sha="stale" * 8, head_ref="feat/x")

    tracker = ConcurrencyTracker()
    for role, cls_name in (
        (BotRole.ARCHITECT, "Architect"),
        (BotRole.SENTINEL, "Sentinel"),
        (BotRole.TESTER, "Tester"),
        (BotRole.SCRIBE, "Scribe"),
    ):
        review_diff = make_tracked_review_diff(_canned(role), tracker)
        monkeypatch.setattr(
            f"repoach.review.orchestrator.{cls_name}",
            lambda db_path=None, role=role, review_diff=review_diff: _boundary_fake_reviewer(
                role, review_diff
            ),
        )

    from repoach.review.orchestrator import resolve_fresh_head

    monkeypatch.setattr(
        "repoach.review.orchestrator.resolve_fresh_head",
        make_tracked_guard(resolve_fresh_head, tracker),
    )
    _silence_downstream_ledger_writes(monkeypatch)

    orch = ReviewTeamOrchestrator(
        forge=gh,
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

    t0 = time.monotonic()
    team = orch.review_pr(pr_number=3)
    wall = time.monotonic() - t0

    assert team.head_sha == "stale" * 8, f"Expected stale head, got {team.head_sha!r}"

    assert_guard_overlapped_fanout(tracker, n_reviewers=4, wall_s=wall)
