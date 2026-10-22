"""``scripts/lint_no_ai_attribution.py`` fails a PR that still carries AI attribution.

SP-CONVENTIONS-GATE G9(c): the CI layer behind the commit-msg hook and
the squash body. Each test builds a throwaway repository, commits with
``--no-verify`` semantics (no hook is installed there) and runs the
script exactly as the CI step does.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "lint_no_ai_attribution.py"
_TRAILER = "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
_FOOTER = "\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)"


@pytest.fixture
def git_env() -> dict[str, str]:
    """An environment isolated from the user's git configuration."""
    return {
        **os.environ,
        "PYTHONPATH": str(_REPO / "src"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.org",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.org",
    }


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
    """Run git in ``repo`` and return stdout, failing the test on error."""
    res = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def _commit(repo: Path, env: dict[str, str], name: str, message: str) -> str:
    """Add a file and commit it with ``message``; return the new SHA."""
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, env, "add", name)
    _git(repo, env, "commit", "-q", "-m", message)
    return _git(repo, env, "rev-parse", "HEAD")


def _lint(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """Run the script from ``repo`` as CI does."""
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def repo(tmp_path: Path, git_env: dict[str, str]) -> Path:
    """A repository with one clean base commit."""
    root = tmp_path / "fixture"
    root.mkdir()
    _git(root, git_env, "init", "-q", "-b", "main")
    _commit(root, git_env, "base.txt", "chore: base")
    return root


def test_a_commit_with_the_trailer_fails_naming_its_sha(
    repo: Path, git_env: dict[str, str]
) -> None:
    """One trailer in the range → rc 1, the finding names the commit."""
    base = _git(repo, git_env, "rev-parse", "HEAD")
    _commit(repo, git_env, "a.txt", "feat(a): clean")
    dirty = _commit(repo, git_env, "b.txt", f"fix(b): dirty\n\n{_TRAILER}")
    res = _lint(repo, git_env, "--range", f"{base}..HEAD")
    assert res.returncode == 1, res.stdout
    assert f"commit {dirty[:12]}: {_TRAILER}" in res.stdout
    assert "1 attribution line(s) found" in res.stdout


def test_a_pr_body_with_the_footer_fails(repo: Path, git_env: dict[str, str]) -> None:
    """``Generated with [Claude Code]`` in the PR body → rc 1."""
    env = {**git_env, "PR_BODY": f"## Summary\n\nFixes the thing.\n\n{_FOOTER}\n"}
    res = _lint(repo, env, "--text-env", "PR_BODY")
    assert res.returncode == 1
    assert f"PR_BODY: {_FOOTER}" in res.stdout


def test_a_clean_range_and_body_pass(repo: Path, git_env: dict[str, str]) -> None:
    """No attribution anywhere → rc 0; a human co-author is fine."""
    base = _git(repo, git_env, "rev-parse", "HEAD")
    _commit(repo, git_env, "c.txt", "feat(c): pair\n\nCo-Authored-By: Ada <ada@example.org>")
    env = {**git_env, "PR_BODY": "Uses the claude_code chain.", "PR_TITLE": "feat(c): pair"}
    res = _lint(
        repo, env, "--range", f"{base}..HEAD", "--text-env", "PR_BODY", "--text-env", "PR_TITLE"
    )
    assert res.returncode == 0, res.stdout
    assert res.stdout.strip().endswith("ai-attribution: ok")


def test_an_unknown_range_is_an_error_not_a_pass(repo: Path, git_env: dict[str, str]) -> None:
    """A range git cannot resolve → rc 2, never a silent ``ok``."""
    res = _lint(repo, git_env, "--range", "deadbeef..HEAD")
    assert res.returncode == 2
    assert "ok" not in res.stdout


def test_ci_runs_the_script_on_the_pr_range_title_and_body() -> None:
    """The CI step judges base..head and both PR texts, on pull_request only."""
    workflow = yaml.safe_load((_REPO / ".github" / "workflows" / "ci.yml").read_text())
    steps = workflow["jobs"]["test"]["steps"]
    step = next(s for s in steps if "lint_no_ai_attribution.py" in str(s.get("run", "")))
    assert step["if"] == "github.event_name == 'pull_request'"
    assert "pull_request.base.sha" in step["run"] and "pull_request.head.sha" in step["run"]
    assert step["env"] == {
        "PR_TITLE": "${{ github.event.pull_request.title }}",
        "PR_BODY": "${{ github.event.pull_request.body }}",
    }
    assert "--text-env PR_TITLE --text-env PR_BODY" in step["run"]
