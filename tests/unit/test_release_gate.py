"""Unit tests for SP-RELEASE-GATE -- the pure release-range classifier and decision core.

The classifier is pinned on the squash-subject suffix pattern; the
decision function is pinned on every red-fact condition.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ferova.review.gh_client import GhResult
from ferova.review.release_gate import (
    ReleaseFacts,
    classify_release_range,
    compute_release_decision,
    gather_release_facts,
)


def _facts(**over: object) -> ReleaseFacts:
    base: dict[str, object] = {
        "develop_sha": "abc123",
        "out_of_band_commits": [],
        "remote_sha": "abc123",
        "pr_head_sha": None,
        "ci_green": True,
        "ci_checked": True,
    }
    base.update(over)
    return ReleaseFacts(**base)


def test_release_range_all_squashes() -> None:
    subjects = [
        "Add retry budget knob (#12)",
        "Fix sentinel max tokens (#37)",
        "Wire release gate CLI (#59)",
    ]
    assert classify_release_range(subjects) == []


def test_release_range_flags_non_pr_commit() -> None:
    subjects = [
        "Add retry budget knob (#12)",
        "Hotfix: patch prod config directly",
        "Wire release gate CLI (#59)",
    ]
    flagged = classify_release_range(subjects)
    assert flagged == ["Hotfix: patch prod config directly"]


def test_release_gate_fail_closed_on_red_ci() -> None:
    facts = _facts(ci_green=False)
    decision = compute_release_decision(facts)
    assert decision.merge is False
    assert any("CI" in reason for reason in decision.reasons)


def _gh_stub(*, develop_sha: str, remote_sha: str, pr_head_sha: str | None = None) -> MagicMock:
    gh = MagicMock()
    gh.pr_head_sha.return_value = pr_head_sha

    def _run_git_side(args: list[str]) -> GhResult:
        if args[:2] == ["rev-parse", "develop"]:
            return GhResult(returncode=0, stdout=f"{develop_sha}\n", stderr="", argv=args)
        if args[:1] == ["log"]:
            return GhResult(returncode=0, stdout="", stderr="", argv=args)
        if args[:2] == ["ls-remote", "origin"]:
            stdout = f"{remote_sha}\trefs/heads/develop\n" if remote_sha else ""
            return GhResult(returncode=0, stdout=stdout, stderr="", argv=args)
        return GhResult(returncode=0, stdout="", stderr="", argv=args)

    gh._run_git.side_effect = _run_git_side
    return gh


def test_release_gate_refuses_stale_head(tmp_path: Path) -> None:
    gh = _gh_stub(develop_sha="abc123", remote_sha="def456")

    def _ci_runner(repo_root: Path) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], 0)

    facts = gather_release_facts(repo_root=tmp_path, gh=gh, ci_runner=_ci_runner)
    decision = compute_release_decision(facts)
    assert decision.merge is False
    assert any("abc123" in reason and "def456" in reason for reason in decision.reasons)


def test_release_gate_missing_ci_script_is_error(tmp_path: Path) -> None:
    gh = _gh_stub(develop_sha="a", remote_sha="a")
    with pytest.raises(FileNotFoundError):
        gather_release_facts(repo_root=tmp_path, gh=gh)
