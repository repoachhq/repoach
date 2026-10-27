"""AI attribution is stripped by the commit-msg hook and recognised everywhere.

SP-CONVENTIONS-GATE G9: the pure helpers in :mod:`repoach.conventions`
and the ``.githooks/commit-msg`` hook, the latter run by a real ``git
commit`` in a throwaway fixture repository.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from repoach.conventions.attribution import attribution_lines, strip_attribution
from repoach.conventions.header import HEADER_MAX_CHARS, header_violations

_REPO = Path(__file__).resolve().parents[2]
_HOOK = _REPO / ".githooks" / "commit-msg"

_TRAILERS = (
    "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>",
    "co-authored-by: Claude <noreply@anthropic.com>",
    "\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)",
    "Generated with Claude Code",
    "Claude-Session: https://claude.ai/code/session_123",
)


@pytest.mark.parametrize("trailer", _TRAILERS)
def test_every_attribution_shape_is_recognised(trailer: str) -> None:
    """Each trailer and footer shape is found and removed."""
    message = f"fix(api): drop the stale cache\n\nBody line.\n\n{trailer}\n"
    assert attribution_lines(message) == [trailer]
    assert strip_attribution(message) == "fix(api): drop the stale cache\n\nBody line.\n"


def test_human_coauthor_and_prose_mentions_survive() -> None:
    """A human co-author and a sentence naming Claude are not attribution."""
    message = (
        "feat(llm): route claude code chain first\n\n"
        "The claude_code provider and Claude Code CLI stay first in the chain.\n\n"
        "Co-Authored-By: Ada Lovelace <ada@example.org>\n"
    )
    assert attribution_lines(message) == []
    assert strip_attribution(message) == message


def test_squash_style_body_loses_every_concatenated_trailer() -> None:
    """A body concatenating several commits' trailers comes out clean."""
    body = (
        "* feat(x): one\n\nCo-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>\n\n"
        "* fix(x): two\n\nCo-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>\n"
    )
    assert strip_attribution(body) == "* feat(x): one\n\n* fix(x): two\n"


def test_header_rules() -> None:
    """Type, shape and length are judged; git's own headers are exempt."""
    assert header_violations("fix(hooks): strip ai attribution\n") == []
    assert header_violations("feat(api)!: drop v1\n") == []
    assert header_violations("Merge branch 'develop' into fix/x\n") == []
    assert header_violations("# comment\n\nfix: x\n") == []
    assert len(header_violations("Fix the thing\n")) == 1
    assert header_violations("\n# only a comment\n") == ["the message is empty"]
    long_header = "fix(x): " + "a" * HEADER_MAX_CHARS
    assert header_violations(long_header) == [
        f"header is {len(long_header)} characters; the limit is {HEADER_MAX_CHARS}"
    ]


def _git(repo: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run git in ``repo`` with ``env``, capturing text output."""
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, timeout=60
    )


@pytest.fixture
def hooked_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A fresh repository whose only hook is this checkout's commit-msg."""
    repo = tmp_path / "fixture"
    hooks = repo / ".githooks"
    hooks.mkdir(parents=True)
    shutil.copy2(_HOOK, hooks / "commit-msg")
    env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        "PYTHONPATH": str(_REPO / "src"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.name", "Fixture"),
        ("config", "user.email", "fixture@example.org"),
        ("config", "core.hooksPath", ".githooks"),
    ):
        assert _git(repo, *args, env=env).returncode == 0
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    assert _git(repo, "add", "a.txt", env=env).returncode == 0
    return repo, env


def test_hook_lands_commit_without_the_trailer(hooked_repo: tuple[Path, dict[str, str]]) -> None:
    """A commit carrying the trailer and footer lands, and neither is in history."""
    repo, env = hooked_repo
    message = (
        "fix(core): keep the cache warm\n\nWhy it matters.\n\n"
        "\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)\n\n"
        "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>\n"
    )
    res = _git(repo, "commit", "-q", "-m", message, env=env)
    assert res.returncode == 0, res.stderr
    assert "stripped AI attribution" in res.stderr
    landed = _git(repo, "log", "-1", "--format=%B", env=env).stdout
    assert landed.strip() == "fix(core): keep the cache warm\n\nWhy it matters."


def test_hook_refuses_a_non_conventional_header(
    hooked_repo: tuple[Path, dict[str, str]],
) -> None:
    """A header outside the grammar is refused and nothing is committed."""
    repo, env = hooked_repo
    res = _git(repo, "commit", "-q", "-m", "Update stuff", env=env)
    assert res.returncode != 0
    assert "header does not match" in res.stderr
    assert _git(repo, "rev-parse", "--verify", "HEAD", env=env).returncode != 0


def test_hook_refuses_a_header_over_72_characters(
    hooked_repo: tuple[Path, dict[str, str]],
) -> None:
    """A conventional header longer than 72 characters is refused."""
    repo, env = hooked_repo
    res = _git(repo, "commit", "-q", "-m", "fix(core): " + "x" * 70, env=env)
    assert res.returncode != 0
    assert "the limit is 72" in res.stderr
