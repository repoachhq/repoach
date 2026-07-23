"""Pin the post-push head-propagation guard (tech-debt survey).

A review launched right after ``git push`` was served the pre-push
head on PR #3 — a full round of integrity and findings landed at the
stale SHA. :func:`resolve_fresh_head` re-polls gh while the local
checkout (the ground truth when it sits on the PR's head branch) and
the served SHA disagree, and returns the served value loudly when the
API never catches up.
"""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from repoach.review.orchestrator import (
    ReviewTeamOrchestrator,
    resolve_fresh_head,
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
