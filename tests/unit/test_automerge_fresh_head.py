"""Tests for :func:`resolve_verified_head` (SP-AUTOMERGE-FRESH-HEAD).

PR #50 orphaned-commit incident (2026-07-06): the PR API served a
``headRefOid`` 40+ minutes stale, and ``run_auto_merge`` squash-merged
at that stale head, orphaning a repair commit already sitting on the
real branch tip.  These tests cover the bounded, fail-closed
API-vs-``git ls-remote`` convergence check that closes that hole:
immediate agreement, convergence after a bounded re-poll, and the two
fail-closed paths (persistent mismatch, ``ls-remote`` transport
failure) that must never return a best-guess SHA.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from ferova.review.auto_merge import resolve_verified_head
from ferova.review.gh_client import GhResult

_FRESH_SHA = "abc123def456abc123def456abc123def456abc1"
_STALE_SHA = "1111111111111111111111111111111111111111"
_OTHER_SHA = "2222222222222222222222222222222222222222"


def _ls_remote_result(sha: str, head_ref: str = "feat/x") -> GhResult:
    return GhResult(
        returncode=0,
        stdout=f"{sha}\trefs/heads/{head_ref}\n",
        stderr="",
        argv=["git", "ls-remote", "origin", f"refs/heads/{head_ref}"],
    )


def test_verified_head_match_first_try(tmp_path: Path) -> None:
    gh = MagicMock()
    gh.pr_head_sha.return_value = _FRESH_SHA
    gh._run_git.return_value = _ls_remote_result(_FRESH_SHA)
    sleep = MagicMock()

    sha, reason = resolve_verified_head(
        gh,
        1,
        "feat/x",
        repo_root=tmp_path,
        sleep=sleep,
    )

    assert sha == _FRESH_SHA
    assert reason == ""
    sleep.assert_not_called()
    gh._run_git.assert_called_once_with(["ls-remote", "origin", "refs/heads/feat/x"])


def test_verified_head_converges_after_repoll(tmp_path: Path) -> None:
    gh = MagicMock()
    gh.pr_head_sha.side_effect = [_STALE_SHA, _FRESH_SHA]
    gh._run_git.return_value = _ls_remote_result(_FRESH_SHA)
    sleep = MagicMock()

    sha, reason = resolve_verified_head(
        gh,
        1,
        "feat/x",
        repo_root=tmp_path,
        delay_s=5.0,
        sleep=sleep,
    )

    assert sha == _FRESH_SHA
    assert reason == ""
    sleep.assert_called_once_with(5.0)


def test_verified_head_persistent_mismatch_fails_closed(tmp_path: Path) -> None:
    gh = MagicMock()
    gh.pr_head_sha.return_value = _STALE_SHA
    gh._run_git.return_value = _ls_remote_result(_OTHER_SHA)
    sleep = MagicMock()

    sha, reason = resolve_verified_head(
        gh,
        1,
        "feat/x",
        repo_root=tmp_path,
        attempts=4,
        delay_s=1.0,
        sleep=sleep,
    )

    assert sha is None
    assert _STALE_SHA[:12] in reason
    assert _OTHER_SHA[:12] in reason
    assert f"api={_STALE_SHA[:12]}" in reason
    assert f"ls_remote={_OTHER_SHA[:12]}" in reason
    assert sleep.call_count == 3


def test_verified_head_ls_remote_error_fails_closed(tmp_path: Path) -> None:
    gh = MagicMock()
    gh.pr_head_sha.return_value = _FRESH_SHA
    gh._run_git.return_value = GhResult(
        returncode=1,
        stdout="",
        stderr="fatal: unable to access origin: network unreachable",
        argv=["git", "ls-remote", "origin", "refs/heads/feat/x"],
    )
    sleep = MagicMock()

    sha, reason = resolve_verified_head(
        gh,
        1,
        "feat/x",
        repo_root=tmp_path,
        sleep=sleep,
    )

    assert sha is None
    assert "network unreachable" in reason
    sleep.assert_not_called()
