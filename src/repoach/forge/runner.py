"""Subprocess plumbing shared by the forge implementations.

Both forges shell out to a CLI (``gh`` / ``glab``) and to ``git`` in
the local clone. :class:`CommandResult` is the captured outcome of one
invocation; :func:`spawn` is the one place a forge process is started,
so the conformance suite can replace it with a recorded-fixture runner
at a single seam.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandResult:
    """Result of a single subprocess invocation.

    Attributes:
        returncode: Exit code of the subprocess (``-1`` on timeout,
            ``127`` when the binary is missing).
        stdout: Captured stdout.
        stderr: Captured stderr.
        argv: The argv that was run; useful for audit logs.
    """

    returncode: int
    stdout: str
    stderr: str
    argv: list[str]

    @property
    def ok(self) -> bool:
        """True when the command exited with code 0."""
        return self.returncode == 0


def spawn(
    argv: list[str],
    *,
    cwd: Path,
    timeout_s: int,
    input_data: str | None = None,
) -> CommandResult:
    """Run ``argv`` in ``cwd`` and capture its output.

    Args:
        argv: Full argv to spawn.
        cwd: Working directory.
        timeout_s: Per-call timeout; a timeout yields ``returncode == -1``.
        input_data: Optional stdin payload.

    Returns:
        The captured :class:`CommandResult`; never raises on a non-zero exit.
    """
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            input=input_data,
        )
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout if isinstance(exc.stdout, str) else ""
        return CommandResult(
            returncode=-1,
            stdout=partial,
            stderr=f"timeout after {timeout_s}s",
            argv=argv,
        )
    return CommandResult(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        argv=argv,
    )
