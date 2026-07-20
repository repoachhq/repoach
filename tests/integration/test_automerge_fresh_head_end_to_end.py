"""End-to-end integration test for :func:`resolve_verified_head` (step 4/4).

SP-AUTOMERGE-FRESH-HEAD, PR #50 orphaned-commit incident: this test
drives the real bounded API-vs-``git ls-remote`` convergence check
against a real, hermetic bare-origin + clone pair (no network beyond
the local bare repo, no ``.env`` reliance, no sleeps).

Operator rule -- no stubs: the fake ``gh`` object used here is a
TRUTHFUL boundary fake. Its ``_run_git`` delegates to a REAL
``GhCli(cwd=work_dir)._run_git`` so ``git ls-remote origin
refs/heads/<branch>`` genuinely talks to the local bare origin over
the filesystem transport. Only ``pr_head_sha`` -- the one call whose
real implementation would hit the live GitHub API -- is scripted by
the test, exactly like the PR API being stale. ``resolve_verified_head``
itself is never patched; it runs for real end to end.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from repoach.review.auto_merge import resolve_verified_head
from repoach.review.gh_client import GhCli

_BRANCH = "feat/fresh-head-e2e"
_ABSENT_SHA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _rev_parse(cwd: Path, ref: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", ref], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


class _ScriptedPrHeadGhCli:
    """Truthful boundary fake for gh: real git via ``GhCli``, scripted API head.

    ``_run_git`` delegates to a real :class:`GhCli` rooted at the clone,
    so ``git ls-remote`` is a genuine local-transport git call against
    the bare origin -- ground truth, unmocked. ``pr_head_sha`` is the
    one seam the test controls, standing in for the PR API's
    ``headRefOid`` the way the 2026-07-06 incident served a stale
    value from that exact call.
    """

    def __init__(self, work_dir: Path, scripted_heads: list[str]) -> None:
        self._real = GhCli(cwd=work_dir)
        self._scripted_heads = list(scripted_heads)

    def _run_git(self, args: list[str]):
        return self._real._run_git(args)

    def pr_head_sha(self, pr_number: int) -> str:
        del pr_number
        if len(self._scripted_heads) > 1:
            return self._scripted_heads.pop(0)
        return self._scripted_heads[0]


def test_stale_head_refused_end_to_end(tmp_path: Path) -> None:
    origin_dir = tmp_path / "origin.git"
    origin_dir.mkdir()
    _run_git(["init", "--bare", "-q"], origin_dir)

    work_dir = tmp_path / "work"
    _run_git(["clone", "-q", str(origin_dir), str(work_dir)], tmp_path)
    _run_git(["checkout", "-q", "-b", _BRANCH], work_dir)
    (work_dir / "readme.txt").write_text("hello\n", encoding="utf-8")
    _run_git(["add", "readme.txt"], work_dir)
    _run_git(
        ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "seed"],
        work_dir,
    )
    _run_git(["push", "-q", "origin", _BRANCH], work_dir)

    real_tip = _rev_parse(work_dir, _BRANCH)
    assert real_tip != _ABSENT_SHA

    stale_gh = _ScriptedPrHeadGhCli(work_dir, [_ABSENT_SHA])
    sha, reason = resolve_verified_head(
        stale_gh,
        1,
        _BRANCH,
        repo_root=work_dir,
        attempts=2,
        delay_s=0,
        sleep=lambda _seconds: None,
    )

    assert sha is None
    assert f"api={_ABSENT_SHA[:12]}" in reason
    assert f"ls_remote={real_tip[:12]}" in reason

    converged_gh = _ScriptedPrHeadGhCli(work_dir, [real_tip])
    sha2, reason2 = resolve_verified_head(
        converged_gh,
        1,
        _BRANCH,
        repo_root=work_dir,
        attempts=2,
        delay_s=0,
        sleep=lambda _seconds: None,
    )

    assert reason2 == ""
    assert sha2 == real_tip
    assert sha2 == _rev_parse(work_dir, _BRANCH)
