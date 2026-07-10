"""SP-RELEASE-GATE integration (AC6) -- end-to-end gate in a throwaway git repo.

Builds a real bare ``origin`` repo and a real clone, seeds a clean
squash-shaped ``develop`` range off ``main``, and drives the actual
``gather_release_facts`` / ``compute_release_decision`` pair against it
(only ``gh._run_git`` paths are exercised -- no network, no ``gh``
binary required). Confirms the gate passes on the clean range, then
proves it refuses the instant an out-of-band commit lands on
``develop``, naming that commit's subject in the reasons.

Also proves the sanctioned merge-commit release shape end to end
(SP-RELEASE-VERIFY-MERGE-COMMIT): a real ``git merge --no-ff`` of
``develop`` into ``main`` verifies True, and a subsequent divergence of
``develop`` past the merged head verifies False.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ferova.review.gh_client import GhCli
from ferova.review.release_gate import (
    compute_release_decision,
    gather_release_facts,
    verify_release,
    write_gate_receipt,
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    return (proc.stdout + proc.stderr).strip()


def _fake_ci_runner(repo_root: Path) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0)


def test_release_gate_clean_range_then_out_of_band_refusal(tmp_path: Path) -> None:
    """Gate passes on a clean squash-shaped range, refuses after an out-of-band commit.

    Steps:
    1. Init a bare ``origin`` repo with ``main`` as the default branch.
    2. Clone it to a working tree and configure a commit identity.
    3. Commit once on ``main`` and push it.
    4. Branch ``develop`` off ``main``, add two ``(#N)``-suffixed commits
       (squash-shaped), push ``develop``.
    5. Gather facts and compute the decision: expect ``merge is True``
       and no reasons.
    6. Add one more commit on ``develop`` whose subject does NOT end in
       ``(#N)`` (an out-of-band commit), push it.
    7. Re-gather facts and recompute the decision: expect
       ``merge is False`` with the out-of-band subject named in reasons.
    """
    origin_dir = tmp_path / "origin.git"
    work_dir = tmp_path / "work"

    _git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin_dir))
    _git(tmp_path, "clone", "-q", str(origin_dir), str(work_dir))
    _git(work_dir, "config", "user.email", "test@example.invalid")
    _git(work_dir, "config", "user.name", "Test Runner")

    (work_dir / "README.md").write_text("hello\n", encoding="utf-8")
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", "chore: init")
    _git(work_dir, "push", "-q", "-u", "origin", "main")

    _git(work_dir, "switch", "-c", "develop")
    for n in (2, 3):
        (work_dir / f"feature_{n}.txt").write_text(f"feature {n}\n", encoding="utf-8")
        _git(work_dir, "add", "-A")
        _git(work_dir, "commit", "-q", "-m", f"Add feature {n} (#{n})")
    _git(work_dir, "push", "-q", "-u", "origin", "develop")

    gh = GhCli(cwd=work_dir)

    facts = gather_release_facts(repo_root=work_dir, gh=gh, ci_runner=_fake_ci_runner)
    decision = compute_release_decision(facts)
    assert decision.merge is True
    assert decision.reasons == []

    out_of_band_subject = "Hotfix: patch prod config directly"
    (work_dir / "hotfix.txt").write_text("hotfix\n", encoding="utf-8")
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", out_of_band_subject)
    _git(work_dir, "push", "-q", "origin", "develop")

    stale_facts = gather_release_facts(repo_root=work_dir, gh=gh, ci_runner=_fake_ci_runner)
    stale_decision = compute_release_decision(stale_facts)
    assert stale_decision.merge is False
    assert any(out_of_band_subject in reason for reason in stale_decision.reasons)


def test_release_verify_merge_commit_end_to_end(tmp_path: Path) -> None:
    """End-to-end proof that a sanctioned merge commit verifies True, then False on drift.

    Builds a real bare ``origin`` plus a work clone, gates the
    ``develop`` head through the real ``gather_release_facts`` /
    ``compute_release_decision`` / ``write_gate_receipt`` chain (with a
    truthful fake ``ci_runner`` reporting green, exactly as the
    clean-range test above does), then performs the exact sanctioned
    merge shape -- a real ``git merge --no-ff`` of ``develop`` into
    ``main`` -- and pushes it. ``verify_release`` against the real
    receipt and a real ``GhCli`` must report verified True with a
    detail naming the match.

    ``develop`` then advances one more commit after the merge landed
    (a stale merge: ``main..develop`` no longer zero), and a second
    ``verify_release`` over the same receipt must report verified
    False.
    """
    origin_dir = tmp_path / "origin.git"
    work_dir = tmp_path / "work"

    _git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin_dir))
    _git(tmp_path, "clone", "-q", str(origin_dir), str(work_dir))
    _git(work_dir, "config", "user.email", "test@example.invalid")
    _git(work_dir, "config", "user.name", "Test Runner")

    (work_dir / "README.md").write_text("hello\n", encoding="utf-8")
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", "chore: init")
    _git(work_dir, "push", "-q", "-u", "origin", "main")

    _git(work_dir, "switch", "-c", "develop")
    (work_dir / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", "Add feature (#1)")
    _git(work_dir, "push", "-q", "-u", "origin", "develop")

    gh = GhCli(cwd=work_dir)

    facts = gather_release_facts(repo_root=work_dir, gh=gh, ci_runner=_fake_ci_runner)
    decision = compute_release_decision(facts)
    assert decision.merge is True
    assert decision.reasons == []

    receipt_path = tmp_path / "release_gate_receipt.json"
    write_gate_receipt(receipt_path, develop_sha=facts.develop_sha, decision=decision)

    _git(work_dir, "switch", "main")
    _git(work_dir, "merge", "--no-ff", "-q", "-m", "Merge develop into main", "develop")
    _git(work_dir, "push", "-q", "origin", "main")

    result = verify_release(receipt_path, gh=gh)
    assert result.verified is True
    assert "matches the approved develop head" in result.detail

    _git(work_dir, "switch", "develop")
    (work_dir / "feature2.txt").write_text("feature 2\n", encoding="utf-8")
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", "Add feature 2 (#2)")
    _git(work_dir, "push", "-q", "origin", "develop")

    stale_result = verify_release(receipt_path, gh=gh)
    assert stale_result.verified is False
