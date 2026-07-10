"""Unit tests for SP-RELEASE-GATE -- the pure release-range classifier and decision core.

The classifier is pinned on the squash-subject suffix pattern; the
decision function is pinned on every red-fact condition.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ferova.review.gh_client import GhCli, GhResult
from ferova.review.release_gate import (
    ReleaseDecision,
    ReleaseFacts,
    classify_release_range,
    compute_release_decision,
    gather_release_facts,
    verify_release,
    write_gate_receipt,
)


def _git(repo: Path, *args: str) -> str:
    """Run ``git`` in *repo* and return combined stdout+stderr stripped.

    Mirrors the helper in ``tests/integration/test_release_gate_end_to_end.py``
    so the throwaway-repo tests share one fixture style.
    """
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    return (proc.stdout + proc.stderr).strip()


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


def test_release_verify_detects_squash_divergence(tmp_path: Path) -> None:
    receipt_path = tmp_path / "release_gate_receipt.json"
    decision = ReleaseDecision(merge=True, reasons=[])
    write_gate_receipt(receipt_path, develop_sha="deadbeef", decision=decision)

    gh = MagicMock()

    def _run_git_side(args: list[str]) -> GhResult:
        if args[:2] == ["ls-remote", "origin"]:
            return GhResult(
                returncode=0,
                stdout="cafef00d\trefs/heads/main\n",
                stderr="",
                argv=args,
            )
        return GhResult(returncode=0, stdout="", stderr="", argv=args)

    gh._run_git.side_effect = _run_git_side

    result = verify_release(receipt_path, gh=gh)

    assert result.verified is False
    assert "squash" in result.detail or "stale" in result.detail


def test_release_gate_never_calls_merge() -> None:
    source = Path(__file__).resolve().parents[2] / "src" / "ferova" / "review" / "release_gate.py"
    text = source.read_text(encoding="utf-8")
    assert "pr merge" not in text
    assert "gh pr merge" not in text
    assert "git push" not in text


def _init_origin_and_work(tmp_path: Path) -> tuple[Path, Path]:
    """Build a bare ``origin`` repo and a configured work clone.

    Mirrors the fixture in ``tests/integration/test_release_gate_end_to_end.py``:
    a bare ``origin`` with ``main`` as the default branch, cloned into a
    working tree with a throwaway commit identity configured.
    """
    origin_dir = tmp_path / "origin.git"
    work_dir = tmp_path / "work"
    _git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin_dir))
    _git(tmp_path, "clone", "-q", str(origin_dir), str(work_dir))
    _git(work_dir, "config", "user.email", "test@example.invalid")
    _git(work_dir, "config", "user.name", "Test Runner")
    return origin_dir, work_dir


def test_verify_accepts_merge_commit_release(tmp_path: Path) -> None:
    """A sanctioned ``git merge --no-ff`` of develop into main verifies True.

    Builds a real throwaway origin/work pair, seeds ``main`` with an
    initial commit, branches ``develop`` with one extra commit, then
    performs the exact merge shape ``release gate`` prescribes --
    "Create a merge commit" -- and pushes. The gate receipt records the
    develop tip (as :func:`write_gate_receipt` does in real operation).
    """
    _origin_dir, work_dir = _init_origin_and_work(tmp_path)

    (work_dir / "README.md").write_text("hello\n", encoding="utf-8")
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", "chore: init")
    _git(work_dir, "push", "-q", "-u", "origin", "main")

    _git(work_dir, "switch", "-c", "develop")
    (work_dir / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", "Add feature (#1)")
    _git(work_dir, "push", "-q", "-u", "origin", "develop")
    develop_sha = _git(work_dir, "rev-parse", "develop")

    _git(work_dir, "switch", "main")
    _git(work_dir, "merge", "--no-ff", "-q", "-m", "Merge develop into main", "develop")
    _git(work_dir, "push", "-q", "origin", "main")

    receipt_path = tmp_path / "release_gate_receipt.json"
    write_gate_receipt(
        receipt_path,
        develop_sha=develop_sha,
        decision=ReleaseDecision(merge=True, reasons=[]),
    )

    gh = GhCli(cwd=work_dir)
    result = verify_release(receipt_path, gh=gh)

    assert result.verified is True


def test_verify_still_refuses_squash(tmp_path: Path) -> None:
    """A squash-style advance of main (not a merge of develop) verifies False.

    Main advances by a plain commit that is neither the approved SHA
    nor a merge commit whose second parent is the approved SHA -- the
    shape a squash merge (or an unrelated commit) produces.
    """
    _origin_dir, work_dir = _init_origin_and_work(tmp_path)

    (work_dir / "README.md").write_text("hello\n", encoding="utf-8")
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", "chore: init")
    _git(work_dir, "push", "-q", "-u", "origin", "main")

    _git(work_dir, "switch", "-c", "develop")
    (work_dir / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", "Add feature (#1)")
    _git(work_dir, "push", "-q", "-u", "origin", "develop")
    develop_sha = _git(work_dir, "rev-parse", "develop")

    _git(work_dir, "switch", "main")
    (work_dir / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", "Add feature (#1) (squashed)")
    _git(work_dir, "push", "-q", "origin", "main")

    receipt_path = tmp_path / "release_gate_receipt.json"
    write_gate_receipt(
        receipt_path,
        develop_sha=develop_sha,
        decision=ReleaseDecision(merge=True, reasons=[]),
    )

    gh = GhCli(cwd=work_dir)
    result = verify_release(receipt_path, gh=gh)

    assert result.verified is False
    assert "squash" in result.detail or "stale" in result.detail


def test_verify_refuses_stale_merge(tmp_path: Path) -> None:
    """A sanctioned merge commit taken, then develop advances -- distance != 0.

    After ``git merge --no-ff`` lands the approved develop head on
    main, develop moves again before verify runs, so
    ``main..develop`` is no longer zero and the merge is stale.
    """
    _origin_dir, work_dir = _init_origin_and_work(tmp_path)

    (work_dir / "README.md").write_text("hello\n", encoding="utf-8")
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", "chore: init")
    _git(work_dir, "push", "-q", "-u", "origin", "main")

    _git(work_dir, "switch", "-c", "develop")
    (work_dir / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", "Add feature (#1)")
    _git(work_dir, "push", "-q", "-u", "origin", "develop")
    develop_sha = _git(work_dir, "rev-parse", "develop")

    _git(work_dir, "switch", "main")
    _git(work_dir, "merge", "--no-ff", "-q", "-m", "Merge develop into main", "develop")
    _git(work_dir, "push", "-q", "origin", "main")

    _git(work_dir, "switch", "develop")
    (work_dir / "feature2.txt").write_text("feature 2\n", encoding="utf-8")
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", "Add feature 2 (#2)")
    _git(work_dir, "push", "-q", "origin", "develop")

    receipt_path = tmp_path / "release_gate_receipt.json"
    write_gate_receipt(
        receipt_path,
        develop_sha=develop_sha,
        decision=ReleaseDecision(merge=True, reasons=[]),
    )

    gh = GhCli(cwd=work_dir)
    result = verify_release(receipt_path, gh=gh)

    assert result.verified is False
    assert "squash" in result.detail or "stale" in result.detail
