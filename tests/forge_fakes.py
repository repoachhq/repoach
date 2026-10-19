"""Truthful boundary fakes for the ``Forge`` protocol, shared by both suites.

Two shapes, both faking only what crosses the process boundary:

* :func:`scripted_github` — the real :class:`~repoach.forge.github.GitHubForge`
  whose ``gh`` and ``git`` subprocess seams (``_run`` / ``_run_git``)
  are :class:`~unittest.mock.MagicMock` objects driven by a side-effect
  callable. Every protocol method runs its real argv construction and
  payload parsing, so a test pins the exact ``gh`` calls repoach makes.
* :func:`gh_ok` / :func:`pr_view_stdout` — canned results for those seams.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from unittest.mock import MagicMock

from repoach.forge.github import GitHubForge
from repoach.forge.runner import CommandResult

RunSide = Callable[[list[str]], CommandResult]


def gh_ok(stdout: str = "", *, argv: list[str] | None = None) -> CommandResult:
    """A successful ``gh`` / ``git`` result carrying ``stdout``."""
    return CommandResult(returncode=0, stdout=stdout, stderr="", argv=argv or ["gh"])


def gh_fail(stderr: str = "boom", *, returncode: int = 1) -> CommandResult:
    """A failed ``gh`` / ``git`` result carrying ``stderr``."""
    return CommandResult(returncode=returncode, stdout="", stderr=stderr, argv=["gh"])


def pr_view_stdout(
    number: int = 1,
    *,
    head_ref: str = "feat/x",
    base_ref: str = "develop",
    state: str = "OPEN",
    head_sha: str = "",
    title: str = "",
    body: str = "",
    author: str = "dev",
) -> str:
    """The JSON ``gh pr view --json …`` prints for a PR with these fields."""
    return json.dumps(
        {
            "number": number,
            "title": title,
            "body": body,
            "headRefName": head_ref,
            "baseRefName": base_ref,
            "author": {"login": author},
            "state": state,
            "url": f"https://github.com/o/r/pull/{number}",
            "headRefOid": head_sha,
            "isDraft": False,
        }
    )


def scripted_github(
    run: RunSide | None = None,
    git: RunSide | None = None,
    *,
    bot_login: str = "github-actions[bot]",
) -> GitHubForge:
    """A real :class:`GitHubForge` whose subprocess seams are scripted mocks.

    Args:
        run: Answers each ``gh`` argv (without the binary); default: empty success.
        git: Answers each ``git`` argv; default: empty success.
        bot_login: The trusted bot identity.

    Returns:
        The forge; ``forge._run`` / ``forge._run_git`` are the mocks, so
        ``call_args_list`` lists every argv the code under test sent.
    """
    forge = GitHubForge(gh_path="/nonexistent/gh", bot_login=bot_login)
    forge._run = MagicMock(
        side_effect=lambda args, input_data=None: (run or (lambda a: gh_ok(argv=a)))(args)
    )
    forge._run_git = MagicMock(side_effect=git or (lambda a: gh_ok(argv=a)))
    return forge
