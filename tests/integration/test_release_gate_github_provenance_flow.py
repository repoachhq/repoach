"""SP-RELEASE-PROVENANCE-GH-FALLBACK integration (AC6) -- GitHub provenance end to end.

Builds a real bare ``origin`` repo and a real clone with a genuine
``main..develop`` release range, drives ``gather_release_facts`` with
``provenance=ProvenanceSource.GITHUB`` against a ``GhCli`` whose git
operations are all real (``_run_git`` untouched) but whose
``merged_pr_merge_shas`` is overridden to return a controlled set of
merge-commit SHAs -- the one boundary a hermetic test cannot cross
live, since it would otherwise require a real ``gh`` call to GitHub.
Mirrors the hermetic tmp-repo fixture style of
``tests/integration/test_release_gate_end_to_end.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from repoach.review.gh_client import GhCli
from repoach.review.release_gate import (
    ProvenanceSource,
    compute_release_decision,
    gather_release_facts,
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    return (proc.stdout + proc.stderr).strip()


def _fake_ci_runner(repo_root: Path) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0)


class _GithubMergedShasGhCli(GhCli):
    """A real ``GhCli`` whose ``merged_pr_merge_shas`` is a settable boundary fake.

    Every other method (``_run_git``, ``pr_head_sha``) runs unchanged
    against the real git repository at ``cwd`` -- only the GitHub
    merged-PR-list call, which would otherwise require network access
    to GitHub, is replaced with a canned set the test controls.
    """

    def __init__(self, *, cwd: Path, merge_commit_shas: set[str]) -> None:
        super().__init__(cwd=cwd)
        self.merge_commit_shas = merge_commit_shas

    def merged_pr_merge_shas(self, base: str) -> set[str]:
        return self.merge_commit_shas


def test_github_provenance_gate_merges_clean_range_then_refuses_on_missing_sha(
    tmp_path: Path,
) -> None:
    """A clean squash-shaped range with every SHA GitHub-covered merges; refuses if one is dropped.

    Steps:
    1. Init a bare ``origin`` repo with ``main`` as the default branch.
    2. Clone it, configure a commit identity, commit once on ``main``
       and push.
    3. Branch ``develop`` off ``main``, add two squash-shaped commits,
       push ``develop``, and record both commits' real SHAs.
    4. Drive ``gather_release_facts(..., provenance=ProvenanceSource.GITHUB)``
       against a fake ``gh`` whose ``merged_pr_merge_shas`` returns
       exactly those two SHAs and a green injected CI runner: expect
       ``merge is True``.
    5. Drop one SHA from the fake GitHub merged-set and re-gather:
       expect the range to refuse, naming the now-uncovered commit.
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
    commit_shas: dict[str, str] = {}
    for n in (2, 3):
        (work_dir / f"feature_{n}.txt").write_text(f"feature {n}\n", encoding="utf-8")
        _git(work_dir, "add", "-A")
        subject = f"Add feature {n} (#{n})"
        _git(work_dir, "commit", "-q", "-m", subject)
        commit_shas[subject] = _git(work_dir, "rev-parse", "HEAD")
    _git(work_dir, "push", "-q", "-u", "origin", "develop")

    all_merged_shas = set(commit_shas.values())
    gh = _GithubMergedShasGhCli(cwd=work_dir, merge_commit_shas=set(all_merged_shas))

    facts = gather_release_facts(
        repo_root=work_dir,
        gh=gh,
        ci_runner=_fake_ci_runner,
        provenance=ProvenanceSource.GITHUB,
    )
    decision = compute_release_decision(facts)

    assert facts.provenance_error is None
    assert facts.out_of_band_commits == []
    assert decision.merge is True
    assert decision.reasons == []

    dropped_subject, dropped_sha = next(iter(commit_shas.items()))
    gh.merge_commit_shas = all_merged_shas - {dropped_sha}

    stale_facts = gather_release_facts(
        repo_root=work_dir,
        gh=gh,
        ci_runner=_fake_ci_runner,
        provenance=ProvenanceSource.GITHUB,
    )
    stale_decision = compute_release_decision(stale_facts)

    assert stale_facts.out_of_band_commits == [dropped_subject]
    assert stale_decision.merge is False
    assert any(dropped_subject in reason for reason in stale_decision.reasons)
