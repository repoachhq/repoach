"""Integration test: the fresh-head guard runs concurrently with the reviewer fan-out.

Exercises ``ReviewTeamOrchestrator.review_pr`` end-to-end (SP-FRESH-HEAD-CONCURRENT)
against a real scratch git repository under ``tmp_path``, a truthful ``GhCli`` fake,
and REAL ``Architect`` / ``Sentinel`` / ``Tester`` / ``Scribe`` instances whose only
replaced surface is the public ``review_diff`` contract method — never an ad-hoc stub
class with an invented signature. Asserts ``head_sha`` resolves correctly and that the
guard's wait overlapped the reviewer fan-out instead of preceding it.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from repoach.review.merge_gate import MergeFacts
from repoach.review.orchestrator import ReviewTeamOrchestrator
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


def _make_sleepy_review_diff(
    outcome: ReviewerOutcome, delay: float
) -> Callable[..., ReviewerOutcome]:
    """Return a review_diff replacement that sleeps *delay* seconds first."""

    def _review_diff(diff: str, **kwargs: object) -> ReviewerOutcome:
        time.sleep(delay)
        return outcome

    return _review_diff


class _GhCliFake:
    """Truthful GhCli replacement that surfaces a stale-forever head SHA.

    The scratch git repo has its own HEAD; this fake always returns a different SHA
    (``\"stale\" * 8``) from ``pr_head_sha`` while reporting the branch name correctly
    from ``pr_view``, which is exactly the condition that makes ``resolve_fresh_head``
    re-poll until it exhausts its attempts and gives up loudly.
    """

    def __init__(self, *, head_sha: str = "stale" * 8, head_ref: str = "feat/x") -> None:
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
    """Neutralise every real-DB side effect review_pr performs past the fan-out.

    None of these functions are the subject under test — SP-FRESH-HEAD-CONCURRENT is
    a scheduling change to the call site, not to findings persistence — so replacing
    them keeps the wall-clock assertion honest about what it measures instead of
    entangling it with incidental SQLite commit latency.
    """
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


def test_concurrent_head_guard_integration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The head guard resolves correctly and overlaps the reviewer fan-out end to end.

    Constructs an orchestrator with a real scratch git repo under ``tmp_path``, a
    truthful stale-forever ``_GhCliFake``, the real, unmodified ``resolve_fresh_head``
    (sped up via its ``delay_s`` keyword-only default), and boundary-fake reviewers
    that each sleep briefly. Asserts ``head_sha`` is the stale SHA the fake returns
    after the guard exhausts its retries, and that total wall time sits below the
    serial sum of guard time plus reviewer time (confirming overlap).
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

    gh = _GhCliFake(head_sha="stale" * 8, head_ref="feat/x")

    reviewer_delay = 1.5
    for role, cls_name in (
        (BotRole.ARCHITECT, "Architect"),
        (BotRole.SENTINEL, "Sentinel"),
        (BotRole.TESTER, "Tester"),
        (BotRole.SCRIBE, "Scribe"),
    ):
        review_diff = _make_sleepy_review_diff(_canned(role), reviewer_delay)
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

    guard_time = 6 * 0.2
    serial_sum = guard_time + reviewer_delay
    concurrent_floor = max(guard_time, reviewer_delay)

    t0 = time.monotonic()
    team = orch.review_pr(pr_number=3)
    wall = time.monotonic() - t0

    assert team.head_sha == "stale" * 8, f"Expected stale head, got {team.head_sha!r}"

    threshold = (concurrent_floor + serial_sum) / 2
    assert wall < threshold, (
        f"Wall time {wall:.2f}s >= {threshold:.2f}s (halfway between the "
        f"{concurrent_floor:.2f}s concurrent floor and the {serial_sum:.2f}s serial "
        "sum); the guard did NOT overlap the reviewer fan-out"
    )
