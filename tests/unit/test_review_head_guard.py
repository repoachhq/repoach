"""Pin the post-push head-propagation guard (tech-debt survey).

A review launched right after ``git push`` was served the pre-push
head on PR #3 — a full round of integrity and findings landed at the
stale SHA. :func:`resolve_fresh_head` re-polls gh while the local
checkout (the ground truth when it sits on the PR's head branch) and
the served SHA disagree, and returns the served value loudly when the
API never catches up.

SP-FRESH-HEAD-CONCURRENT adds coverage for the call site itself: the
guard is submitted to a background pool instead of blocking the
reviewer fan-out, and joined exactly once at its first real consumer.
Every reviewer double in this module is a REAL ``Architect`` /
``Sentinel`` / ``Tester`` / ``Scribe`` instance with only its public
``review_diff`` contract method replaced — never an ad-hoc stub class
with an invented signature — so the fan-out call site
(``r.review_diff(diff, pr_number=..., ...)``) is exercised exactly as
in production.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from repoach.review.merge_gate import MergeFacts
from repoach.review.orchestrator import (
    ReviewTeamOrchestrator,
    resolve_fresh_head,
)
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


@pytest.fixture
def repo_on_branch(tmp_path: Path) -> tuple[Path, str]:
    """A scratch repo with one commit on branch feat/x; returns (root, head)."""
    subprocess.run(["git", "init", "-q", "-b", "feat/x", str(tmp_path)], check=True)
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
            "seed",
        ],
        cwd=tmp_path,
        check=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()
    return tmp_path, head


def test_retries_until_the_served_head_catches_up(
    repo_on_branch: tuple[Path, str],
) -> None:
    """Stale-then-fresh gh answers converge on the local HEAD."""
    root, local_head = repo_on_branch
    gh = MagicMock()
    gh.pr_view.return_value = {"headRefName": "feat/x"}
    gh.pr_head_sha.side_effect = ["stale" * 8, "stale" * 8, local_head]
    slept: list[float] = []
    resolved = resolve_fresh_head(
        gh, 3, repo_root=root, attempts=5, delay_s=0.0, sleep=slept.append
    )
    assert resolved == local_head
    assert len(slept) == 2


def test_gives_up_loudly_but_returns_the_served_head(
    repo_on_branch: tuple[Path, str],
) -> None:
    """A never-converging API still yields the served value (evidence-first)."""
    root, _ = repo_on_branch
    gh = MagicMock()
    gh.pr_view.return_value = {"headRefName": "feat/x"}
    gh.pr_head_sha.return_value = "stale" * 8
    resolved = resolve_fresh_head(
        gh, 3, repo_root=root, attempts=2, delay_s=0.0, sleep=lambda _s: None
    )
    assert resolved == "stale" * 8


def test_foreign_branch_checkout_skips_the_guard(
    repo_on_branch: tuple[Path, str],
) -> None:
    """When the checkout is not the PR's head branch, no retry happens."""
    root, _ = repo_on_branch
    gh = MagicMock()
    gh.pr_view.return_value = {"headRefName": "another/branch"}
    gh.pr_head_sha.return_value = "served" * 6
    resolved = resolve_fresh_head(gh, 3, repo_root=root, attempts=5, delay_s=0.0)
    assert resolved == "served" * 6
    assert gh.pr_head_sha.call_count == 1


def test_pr_view_exception_returns_the_served_head(
    repo_on_branch: tuple[Path, str],
) -> None:
    """A gh without pr_view (or a failing one) degrades to the served head."""
    root, _ = repo_on_branch
    gh = MagicMock()
    gh.pr_head_sha.return_value = "served" * 6
    gh.pr_view.side_effect = RuntimeError("boom")
    resolved = resolve_fresh_head(gh, 3, repo_root=root, attempts=3, delay_s=0.0)
    assert resolved == "served" * 6
    assert gh.pr_head_sha.call_count == 1


def test_join_head_guard_returns_none_when_no_pool() -> None:
    """When pool and future are both None, _join_head_guard returns None."""
    orch = ReviewTeamOrchestrator(post_to_github=False)
    result = orch._join_head_guard(None, None, pr_number=42)
    assert result is None


def test_join_head_guard_returns_future_result() -> None:
    """A completed future's result is returned unchanged."""
    orch = ReviewTeamOrchestrator(post_to_github=False)
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(lambda: "abc123")
    result = orch._join_head_guard(pool, future, pr_number=42)
    assert result == "abc123"


def test_join_head_guard_catches_exception_and_logs() -> None:
    """An exception from the future is caught, logged, and degraded to None."""
    orch = ReviewTeamOrchestrator(post_to_github=False)

    def _boom() -> str:
        raise RuntimeError("simulated crash")

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_boom)
    result = orch._join_head_guard(pool, future, pr_number=99)
    assert result is None


_ROLE_TO_CLASS: dict[BotRole, type[Reviewer]] = {
    BotRole.ARCHITECT: Architect,
    BotRole.SENTINEL: Sentinel,
    BotRole.TESTER: Tester,
    BotRole.SCRIBE: Scribe,
}


def _canned_outcome(
    role: BotRole, verdict: ReviewVerdict = ReviewVerdict.APPROVE
) -> ReviewerOutcome:
    """Return a minimal canned ReviewerOutcome for a single role."""
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


def _boundary_fake_reviewer(
    role: BotRole,
    review_diff: Callable[..., ReviewerOutcome],
) -> Reviewer:
    """A real Architect/Sentinel/Tester/Scribe with only review_diff replaced.

    The production reviewer class is instantiated for real (its
    ``AgentLoop`` boundary faked via a ``MagicMock`` that is never
    exercised), keeping ``.role`` and the rest of the object's surface
    truthful. Only the public ``review_diff`` contract — the boundary
    the orchestrator's fan-out actually calls across — is substituted,
    with *review_diff* required to accept the same ``diff`` positional
    argument the real method does.
    """
    instance = _ROLE_TO_CLASS[role](loop=MagicMock())
    instance.review_diff = review_diff
    return instance


def _make_event_setting_review_diff(
    outcome: ReviewerOutcome, event: threading.Event
) -> Callable[..., ReviewerOutcome]:
    """Return a review_diff replacement that sets *event* before returning."""

    def _review_diff(
        diff: str,
        *,
        pr_number: int | None = None,
        spec_plan: str | None = None,
        dialogue_context: object | None = None,
        resolved_disagreements: tuple[object, ...] | None = None,
        prior_review: object | None = None,
        extra_prompt_section: str = "",
        arch_edges: str = "",
    ) -> ReviewerOutcome:
        event.set()
        return outcome

    return _review_diff


def _make_sleepy_review_diff(
    outcome: ReviewerOutcome, delay: float
) -> Callable[..., ReviewerOutcome]:
    """Return a review_diff replacement that sleeps *delay* seconds first."""

    def _review_diff(diff: str, **kwargs: object) -> ReviewerOutcome:
        time.sleep(delay)
        return outcome

    return _review_diff


def _make_gh_stub(*, head_sha: str = "deadbeef", head_ref: str = "feat/x") -> MagicMock:
    """Return a GhCli MagicMock with enough wiring for review_pr to proceed."""
    gh = MagicMock()
    gh.pr_diff.return_value = MagicMock(
        ok=True,
        stdout=("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n+x = 1\n"),
        stderr="",
    )
    gh.pr_view.return_value = {"headRefName": head_ref, "title": "test"}
    gh.pr_head_sha.return_value = head_sha
    gh.list_review_comments.return_value = []
    gh.pr_review_comment.return_value = MagicMock(returncode=0, stdout="", stderr="")
    gh.pr_review_submit.return_value = MagicMock(returncode=0, stdout="", stderr="")
    gh.upsert_archive_comment.return_value = MagicMock(returncode=0, stdout="", stderr="")
    gh.fetch_archive_comment.return_value = None
    return gh


def _init_scratch_repo(root: Path) -> None:
    """git init + one commit on branch feat/x under *root*."""
    root.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "feat/x", str(root)], check=True)
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
        cwd=root,
        check=True,
    )


def _silence_downstream_ledger_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise every real-DB side effect review_pr performs past the fan-out.

    None of these functions are the subject under test — SP-FRESH-HEAD-
    CONCURRENT is a scheduling change to the call site, not to findings
    persistence — so replacing them keeps a wall-clock assertion honest
    about what it measures instead of entangling it with unrelated
    SQLite commit latency.
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


def _patch_reviewer_classes(
    monkeypatch: pytest.MonkeyPatch,
    review_diff_by_role: dict[BotRole, Callable[..., ReviewerOutcome]],
) -> None:
    """Replace Architect/Sentinel/Tester/Scribe with truthful boundary fakes."""
    class_name_by_role = {
        BotRole.ARCHITECT: "Architect",
        BotRole.SENTINEL: "Sentinel",
        BotRole.TESTER: "Tester",
        BotRole.SCRIBE: "Scribe",
    }
    for role, class_name in class_name_by_role.items():
        review_diff = review_diff_by_role[role]
        monkeypatch.setattr(
            f"repoach.review.orchestrator.{class_name}",
            lambda db_path=None, role=role, review_diff=review_diff: _boundary_fake_reviewer(
                role, review_diff
            ),
        )


def _silence_publish_side_effects(
    orch: ReviewTeamOrchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orch, "_fire_routine", lambda _team: None)
    monkeypatch.setattr(orch, "_fire_proposed_escalation_dossier", lambda _pn: None)
    monkeypatch.setattr(orch, "_upsert_archive_comment", lambda _team: None)
    monkeypatch.setattr(orch, "_run_auto_challenge_pass", lambda **kw: None)
    monkeypatch.setattr(orch, "_publish_outcome", lambda **kw: None)


def test_orchestrator_head_guard_call_site_is_non_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The head guard is submitted to a background pool and runs concurrently with reviewers.

    Constructs the orchestrator with an explicit ``repo_root`` that differs from the
    process cwd. Monkeypatches ``resolve_fresh_head`` with a fake that records its
    ``repo_root`` kwarg and waits on an event set by the boundary-fake reviewers,
    returning ``"event-set"`` when the event fires and ``"timed-out"`` otherwise. After
    the fix, the event is set well inside the one-second timeout and
    ``head_sha == "event-set"`` while the recorded repo_root matches the orchestrator's,
    not ``Path.cwd()``.
    """
    repo_root = tmp_path / "worktree"
    _init_scratch_repo(repo_root)

    reviewer_fired = threading.Event()
    recorded_repo_root: list[Path] = []

    def _fake_resolve_fresh_head(
        gh: object,
        pr_number: int,
        *,
        repo_root: Path,
        attempts: int = 6,
        delay_s: float = 5.0,
        sleep: object = time.sleep,
    ) -> str | None:
        recorded_repo_root.append(repo_root)
        if reviewer_fired.wait(timeout=1.0):
            return "event-set"
        return "timed-out"

    monkeypatch.setattr("repoach.review.orchestrator.resolve_fresh_head", _fake_resolve_fresh_head)

    review_diff_by_role = {
        role: _make_event_setting_review_diff(_canned_outcome(role), reviewer_fired)
        for role in _ROLE_TO_CLASS
    }
    _patch_reviewer_classes(monkeypatch, review_diff_by_role)
    _silence_downstream_ledger_writes(monkeypatch)

    gh = _make_gh_stub()
    orch = ReviewTeamOrchestrator(
        gh=gh,
        db_path=tmp_path / "review.db",
        post_to_github=True,
        max_workers=4,
        repo_root=repo_root,
    )
    _silence_publish_side_effects(orch, monkeypatch)

    team = orch.review_pr(pr_number=3)

    assert team.head_sha == "event-set", (
        f"Expected 'event-set' but got {team.head_sha!r}; "
        "the head guard is still blocking the reviewer fan-out"
    )
    assert len(recorded_repo_root) == 1
    assert recorded_repo_root[0] == repo_root, (
        f"resolve_fresh_head received repo_root={recorded_repo_root[0]!r}, expected {repo_root!r}"
    )


def test_orchestrator_head_guard_overlaps_reviewer_fanout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard's wait overlaps with the reviewer fan-out instead of preceding it.

    Drives the real, unmodified ``resolve_fresh_head`` through the real call site
    against a real scratch git repository, with a truthful stale-forever ``GhCli``
    fake so every poll returns a served SHA that never matches local HEAD. Speeds up
    the retry cadence via ``resolve_fresh_head.__kwdefaults__["delay_s"]`` (a
    legitimate override of a keyword-only default; ``attempts`` and the retry
    algorithm are untouched). Boundary-fake reviewers each sleep before returning a
    canned APPROVE outcome. Every downstream ledger write is neutralised (see
    ``_silence_downstream_ledger_writes``) so the wall-clock assertion measures only
    the guard/fan-out overlap, not incidental SQLite commit latency. Asserts total
    elapsed wall time sits comfortably below the serial sum of the guard's six
    retries (``6 * delay_s``) plus the reviewer delay, and close to the concurrent
    floor of ``max(guard_time, reviewer_time)`` — true today only after paying the
    full serial sum, true after the fix once the two phases overlap.
    """
    repo_root = tmp_path / "repo"
    _init_scratch_repo(repo_root)

    gh = _make_gh_stub(head_sha="stale" * 8, head_ref="feat/x")

    reviewer_delay = 1.5
    review_diff_by_role = {
        role: _make_sleepy_review_diff(_canned_outcome(role), reviewer_delay)
        for role in _ROLE_TO_CLASS
    }
    _patch_reviewer_classes(monkeypatch, review_diff_by_role)
    _silence_downstream_ledger_writes(monkeypatch)

    monkeypatch.setitem(resolve_fresh_head.__kwdefaults__, "delay_s", 0.2)

    orch = ReviewTeamOrchestrator(
        gh=gh,
        db_path=tmp_path / "review.db",
        post_to_github=True,
        max_workers=4,
        repo_root=repo_root,
    )
    _silence_publish_side_effects(orch, monkeypatch)

    guard_time = 6 * 0.2
    serial_sum = guard_time + reviewer_delay
    concurrent_floor = max(guard_time, reviewer_delay)

    t0 = time.monotonic()
    team = orch.review_pr(pr_number=3)
    wall = time.monotonic() - t0

    assert team.head_sha == "stale" * 8

    threshold = (concurrent_floor + serial_sum) / 2
    assert wall < threshold, (
        f"Wall time {wall:.2f}s >= {threshold:.2f}s (halfway between the "
        f"{concurrent_floor:.2f}s concurrent floor and the {serial_sum:.2f}s serial "
        "sum); the guard did NOT overlap the reviewer fan-out"
    )
