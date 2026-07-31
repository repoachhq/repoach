"""Unit tests for SP-BRANCH-CONFIG -- configurable integration/release branches.

Pins that the develop/main two-branch model reads its two branch names from
``Settings`` (``integration_branch`` / ``release_branch``) instead of bare
``"develop"`` / ``"main"`` string literals: the default values reproduce
today's behavior byte-for-byte (NG1), while an env override flips the
``auto_merge`` / ``coder_findings`` refusal sites, reshapes the git ref
arguments ``release_gate`` issues, and is honoured by a default-argument
site (``ensure_branch``) resolved at call time rather than frozen at import.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from repoach.core import config
from repoach.review.auto_merge import OUTCOME_SKIP_BASE, run_auto_merge
from repoach.review.coder_findings import run_coder_fix_from_findings
from repoach.review.dev_runner import ensure_branch
from repoach.review.gh_client import GhResult
from repoach.review.release_gate import gather_release_facts, verify_release_live


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Force a fresh :class:`~repoach.core.config.Settings` per test.

    ``get_settings`` caches a process-wide singleton (:data:`config._settings`)
    -- every test in this module either relies on the default branch names or
    sets an env override, so the cache must be cleared both before and after
    each test (mirroring the existing ``config._settings = None`` pattern
    used across the suite, e.g. ``test_automerge_fail_fast_gate.py``).
    """
    config._settings = None
    yield
    config._settings = None


def _all_green_check_runs() -> str:
    import json

    return "\n".join(
        json.dumps({"name": f"check-{i}", "status": "COMPLETED", "conclusion": "SUCCESS"})
        for i in range(3)
    )


def _gh_auto_merge_stub(*, base: str, head: str = "feat/x", state: str = "OPEN") -> MagicMock:
    """Minimal :class:`GhCli` stand-in exercising ``run_auto_merge``'s base check.

    Only detailed enough to reach *past* the base-branch refusal when the
    base matches -- the subsequent pure gate is expected to refuse for an
    unrelated reason (no review-integrity record seeded), which is fine:
    this test only asserts the base check itself flips, not a full merge.
    """
    gh = MagicMock()
    gh.pr_view.return_value = {"baseRefName": base, "headRefName": head, "state": state}
    gh.pr_head_sha.return_value = "head_sha_1234567890"

    def _run_git_side(args: list[str]) -> GhResult:
        if args[:1] == ["ls-remote"]:
            return GhResult(
                returncode=0,
                stdout=f"head_sha_1234567890\trefs/heads/{head}\n",
                stderr="",
                argv=args,
            )
        return GhResult(returncode=0, stdout="", stderr="", argv=args)

    gh._run_git.side_effect = _run_git_side

    def _run_side(args: list[str]) -> GhResult:
        if args[:1] == ["api"] and "/check-runs" in args[1]:
            return GhResult(returncode=0, stdout=_all_green_check_runs(), stderr="", argv=args)
        if args[:2] == ["pr", "view"]:
            return GhResult(returncode=0, stdout="{}", stderr="", argv=args)
        return GhResult(returncode=0, stdout="", stderr="", argv=args)

    gh._run.side_effect = _run_side
    return gh


def test_settings_branch_defaults_with_no_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env override: ``integration_branch``/``release_branch`` stay develop/main (AC1, NG1)."""
    monkeypatch.delenv("REPOACH_INTEGRATION_BRANCH", raising=False)
    monkeypatch.delenv("INTEGRATION_BRANCH", raising=False)
    monkeypatch.delenv("REPOACH_RELEASE_BRANCH", raising=False)
    monkeypatch.delenv("RELEASE_BRANCH", raising=False)

    settings = config.get_settings()

    assert settings.integration_branch == "develop"
    assert settings.release_branch == "main"


def test_settings_branch_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``REPOACH_INTEGRATION_BRANCH``/``REPOACH_RELEASE_BRANCH`` override the defaults (AC1)."""
    monkeypatch.setenv("REPOACH_INTEGRATION_BRANCH", "trunk")
    monkeypatch.setenv("REPOACH_RELEASE_BRANCH", "release")

    settings = config.get_settings()

    assert settings.integration_branch == "trunk"
    assert settings.release_branch == "release"


def test_auto_merge_refusal_flips_to_configured_integration_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With ``integration_branch=trunk``, a ``develop``-based PR is refused

    (naming ``trunk``, the inverse of pre-change behavior) and a
    ``trunk``-based PR clears the base check entirely (AC2).
    """
    monkeypatch.setenv("REPOACH_INTEGRATION_BRANCH", "trunk")

    res_develop = run_auto_merge(
        1, gh=_gh_auto_merge_stub(base="develop"), db_path=tmp_path / "develop.db"
    )
    assert res_develop.outcome == OUTCOME_SKIP_BASE
    assert "trunk" in res_develop.notes
    assert "develop" in res_develop.notes

    res_trunk = run_auto_merge(
        2, gh=_gh_auto_merge_stub(base="trunk"), db_path=tmp_path / "trunk.db"
    )
    assert res_trunk.outcome != OUTCOME_SKIP_BASE


def _gh_coder_findings_stub(*, base: str, head: str = "feat/x") -> MagicMock:
    gh = MagicMock()
    gh.pr_view.return_value = {"baseRefName": base, "headRefName": head, "state": "OPEN"}
    gh.pr_head_sha.return_value = "head123"
    return gh


def test_coder_findings_refusal_flips_to_configured_integration_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mirrors the auto_merge flip for the Coder's own base refusal (AC2)."""
    from repoach.review import coder_loop

    monkeypatch.setenv("REPOACH_INTEGRATION_BRANCH", "trunk")
    monkeypatch.setattr(coder_loop, "fetch_ci_status", lambda *a, **k: (coder_loop.CI_GREEN, []))

    res_develop = run_coder_fix_from_findings(
        1,
        gh=_gh_coder_findings_stub(base="develop"),
        repo_root=tmp_path,
        coder=MagicMock(),
        db_path=tmp_path / "develop.db",
    )
    assert res_develop.wrong_base is True
    assert res_develop.no_op_reason is not None
    assert "trunk" in res_develop.no_op_reason
    assert "develop" in res_develop.no_op_reason

    res_trunk = run_coder_fix_from_findings(
        2,
        gh=_gh_coder_findings_stub(base="trunk"),
        repo_root=tmp_path,
        coder=MagicMock(),
        db_path=tmp_path / "trunk.db",
    )
    assert res_trunk.wrong_base is False


def _release_gh_stub(calls: list[list[str]], *, head_sha: str = "sha1") -> MagicMock:
    gh = MagicMock()
    gh.pr_head_sha.return_value = None

    def _run_git_side(args: list[str]) -> GhResult:
        calls.append(args)
        if args[:1] == ["rev-parse"]:
            return GhResult(returncode=0, stdout=f"{head_sha}\n", stderr="", argv=args)
        if args[:1] == ["log"]:
            return GhResult(returncode=0, stdout="", stderr="", argv=args)
        if args[:1] == ["ls-remote"]:
            ref = args[-1]
            return GhResult(
                returncode=0, stdout=f"{head_sha}\trefs/heads/{ref}\n", stderr="", argv=args
            )
        if args[:1] == ["fetch"]:
            return GhResult(returncode=0, stdout="", stderr="", argv=args)
        if args[:1] == ["rev-list"]:
            return GhResult(returncode=0, stdout="0\n", stderr="", argv=args)
        return GhResult(returncode=0, stdout="", stderr="", argv=args)

    gh._run_git.side_effect = _run_git_side
    return gh


def _noop_ci_runner(repo_root: Path) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0)


def test_release_gate_gather_facts_uses_configured_branch_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``gather_release_facts`` issues its git ref commands against the

    configured integration/release names, not the literal ``develop``/``main``
    (AC3) -- a spy on ``gh._run_git`` records every invocation's argv.
    """
    monkeypatch.setenv("REPOACH_INTEGRATION_BRANCH", "trunk")
    monkeypatch.setenv("REPOACH_RELEASE_BRANCH", "release")
    calls: list[list[str]] = []
    gh = _release_gh_stub(calls)

    gather_release_facts(repo_root=tmp_path, gh=gh, ci_runner=_noop_ci_runner)

    assert ["rev-parse", "trunk"] in calls
    assert ["ls-remote", "origin", "trunk"] in calls
    log_calls = [c for c in calls if c[:1] == ["log"]]
    assert log_calls and log_calls[0][1] == "release..trunk"
    assert not any("develop" in arg for c in calls for arg in c)
    assert not any(arg == "main" for c in calls for arg in c)


def test_release_gate_verify_live_uses_configured_branch_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``verify_release_live``'s fetch/ls-remote/rev-parse/rev-list all read

    the configured names too (AC3), and the sanctioned-shape decision
    itself is unaffected (NG4): a fast-forward (``main_sha == expected_sha``)
    still verifies true.
    """
    monkeypatch.setenv("REPOACH_INTEGRATION_BRANCH", "trunk")
    monkeypatch.setenv("REPOACH_RELEASE_BRANCH", "release")
    calls: list[list[str]] = []
    gh = _release_gh_stub(calls, head_sha="deadbeef")

    result = verify_release_live(gh=gh)

    assert ["fetch", "--quiet", "origin", "release", "trunk"] in calls
    assert ["ls-remote", "origin", "trunk"] in calls
    assert ["ls-remote", "origin", "release"] in calls
    assert ["rev-parse", "origin/release^2"] in calls
    assert ["rev-list", "--count", "origin/release..origin/trunk"] in calls
    assert not any("develop" in arg for c in calls for arg in c)
    assert not any(arg == "main" or arg == "origin/main^2" for c in calls for arg in c)
    assert result.verified is True


def _init_git_repo(root: Path, *, default_branch: str) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    subprocess.run(["git", "branch", "-m", default_branch], cwd=root, check=True)


def test_ensure_branch_default_uses_configured_integration_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``ensure_branch`` with no explicit ``base`` resolves it from settings

    at call time (AC4): a real tmp git repo whose only branch is ``trunk``
    (not ``develop``) can only be branched off successfully once
    ``integration_branch`` is configured to match -- pre-change code, hardcoded
    to ``base="develop"``, fails to find that ref and returns ``False`` here.
    """
    monkeypatch.setenv("REPOACH_INTEGRATION_BRANCH", "trunk")
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo, default_branch="trunk")

    ok = ensure_branch("feat/new-work", repo_root=repo)

    assert ok is True
    current = subprocess.run(
        ["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert current == "feat/new-work"
