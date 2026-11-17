"""The one ``gh`` runner of the affected-jobs harvest (SP-AFFECTED-JOBS-MISSES-ARE-TEST-CASES).

Kept apart from :mod:`.harvest` so the seam the test suite closes,
:func:`run_gh`, is patched without importing the review package, and
so :mod:`.misses` and its replay import no ``gh`` caller.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence

GhRunner = Callable[..., str]
"""Runs ``gh`` with the given arguments and returns its stdout; raises :class:`HarvestError`."""


class HarvestError(RuntimeError):
    """A forge read failed or answered something the harvest cannot use.

    Attributes:
        command: The ``gh`` command line, as run.
        detail: The first line of its stderr, or what was wrong with its answer.
    """

    def __init__(self, command: Sequence[str], detail: str) -> None:
        self.command = " ".join(command)
        self.detail = detail
        super().__init__(f"{self.command}: {detail}")


def run_gh(*args: str) -> str:
    """Run ``gh`` with ``args`` and return its stdout.

    Args:
        *args: The arguments after ``gh``.

    Returns:
        The command's stdout.

    Raises:
        HarvestError: ``gh`` is not installed or exits non-zero; the
            error names the command and the first line of its stderr.
    """
    command = ["gh", *args]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise HarvestError(command, "gh executable not found") from exc
    if completed.returncode != 0:
        lines = completed.stderr.strip().splitlines()
        raise HarvestError(command, lines[0] if lines else f"exit {completed.returncode}")
    return completed.stdout
