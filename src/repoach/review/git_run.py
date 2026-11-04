"""The isolated git runner every local git read of the review goes through.

Shared by the generated-artefact flow (:mod:`repoach.review.generated_git`)
and the base resolution (:mod:`repoach.review.base_ref`), which depend on
it and never on each other: no hook, no fsmonitor, no ``ext::`` remote, no
user or system config and no secret reach git, whatever a check wrote.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .secret_env import scrubbed_env

GIT_ISOLATION: tuple[str, ...] = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "protocol.ext.allow=never",
)
"""Settings no repository or user config may override: no hook, no fsmonitor, no ext:: remote."""


def _git_env() -> dict[str, str]:
    """The environment for git: no user or system config, never a prompt.

    In the verification job a check is pull-request code run by the job's
    user, so it can write that user's ``~/.gitconfig``; git must not read it.
    Local reads need no secret, so git and any helper it spawns get none
    (:func:`~repoach.review.secret_env.scrubbed_env`).
    """
    env = scrubbed_env()
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    return env


def run_git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Run one git command in ``root`` and capture its bytes (never raises on its exit).

    The command reads no user or system config and runs no hook or
    fsmonitor (:data:`GIT_ISOLATION`), whatever a check wrote there.

    Args:
        root: The working directory.
        *args: The git arguments.

    Returns:
        The completed process, stdout and stderr as bytes.
    """
    return subprocess.run(
        ["git", *GIT_ISOLATION, *args],
        cwd=root,
        capture_output=True,
        check=False,
        env=_git_env(),
    )


__all__ = ["GIT_ISOLATION", "run_git"]
