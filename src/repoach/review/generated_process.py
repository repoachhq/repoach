"""How a generated-artefact check runs: its own session, nothing it starts outliving it.

The OS-level supervision of SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ: the
:class:`Runner` seam, and :func:`subprocess_runner`, which never uses a
shell, waits on the check rather than on its output pipes, kills its whole
session when it exits, and adopts and kills any descendant that detached
itself (Linux ``PR_SET_CHILD_SUBREAPER``).
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..core.logging import get_logger

_log = get_logger(__name__)

OUTPUT_READ_MAX_BYTES = 64 * 1024
"""Bytes read back from the end of a check's output: only its tail is ever kept."""


@dataclass(frozen=True)
class RunResult:
    """What a :class:`Runner` returns: the exit code and the merged output."""

    exit_code: int
    output: str


class Runner(Protocol):
    """The subprocess seam ``run_checks`` calls (tests replace it).

    Raises ``subprocess.TimeoutExpired`` when the check outlives ``timeout_s``.
    """

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
        shell: bool,
    ) -> RunResult:
        """Run ``argv`` in ``cwd`` with ``env`` and return its result."""
        ...


def subprocess_runner(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_s: float,
    shell: bool = False,
) -> RunResult:
    """Run one check in its own session, never through a shell, and kill what it leaves.

    Args:
        argv: The interpreter and the declared check, placeholders replaced.
        cwd: The head tree the check runs in.
        env: The environment, already scrubbed of secrets.
        timeout_s: Seconds the check may run.
        shell: Must be False; present so a caller's intent is checked.

    Returns:
        The exit code and the merged stdout and stderr. The check is
        waited on, not its output pipes: a process it leaves behind holds
        no pipe open and is killed with its session.

    Raises:
        ValueError: ``shell`` is True (NG3: no shell, ever).
        subprocess.TimeoutExpired: The check outlived ``timeout_s``.
    """
    if shell:
        raise ValueError("a generated-artefact check never runs through a shell")
    adopting = _adopt_orphans()
    before = _children()
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            proc.wait(timeout=timeout_s)
        finally:
            _kill_group(proc)
            if adopting:
                _kill_orphans(before)
        size = output.seek(0, os.SEEK_END)
        output.seek(max(0, size - OUTPUT_READ_MAX_BYTES))
        text = output.read().decode("utf-8", errors="replace")
    return RunResult(proc.returncode, text)


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """Kill every process the check left in its session, then reap the check.

    The check is pull-request code: a process it leaves behind could
    rewrite the evidence after ``repoach review generated`` wrote it, so
    nothing it started may outlive its run.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        _log.debug("generated.check_group_gone", pid=proc.pid)
    proc.wait()


_PR_SET_CHILD_SUBREAPER = 36
"""``prctl`` option making this process adopt its orphaned descendants (Linux)."""

_ORPHAN_SWEEPS = 50
_ORPHAN_SWEEP_PAUSE_S = 0.02


def _adopt_orphans() -> bool:
    """Make this process the subreaper of its descendants; False where the kernel cannot.

    A process the check detaches from its session (``setsid``, a double
    fork) escapes :func:`_kill_group`; as a subreaper, this process
    adopts it when its parent dies, and :func:`_kill_orphans` kills it.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return bool(libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0)
    except (OSError, AttributeError) as exc:
        _log.warning("generated.subreaper_unavailable", exc=type(exc).__name__)
        return False


def _children() -> set[int]:
    """Return the pids whose parent is this process (read from ``/proc``)."""
    me = str(os.getpid())
    return {
        int(entry.name)
        for entry in Path("/proc").iterdir()
        if entry.name.isdigit() and _parent_of(entry) == me
    }


def _parent_of(proc_dir: Path) -> str:
    """Return the parent pid ``/proc/<pid>/stat`` names, empty when the process is gone."""
    try:
        stat = (proc_dir / "stat").read_text()
    except OSError as exc:
        _log.debug("generated.proc_gone", pid=proc_dir.name, exc=type(exc).__name__)
        return ""
    fields = stat.rsplit(")", 1)[-1].split()
    return fields[1] if len(fields) > 1 else ""


def _kill_orphans(before: set[int]) -> None:
    """Kill and reap every child adopted since ``before``, until none is left.

    Sweeps a few times: a grandchild reaches this process only once its
    own parent has died.
    """
    quiet = 0
    for _ in range(_ORPHAN_SWEEPS):
        adopted = _children() - before
        if not adopted:
            quiet += 1
            if quiet >= 3:
                return
            time.sleep(_ORPHAN_SWEEP_PAUSE_S)
            continue
        quiet = 0
        for pid in adopted:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except (ProcessLookupError, ChildProcessError):
                _log.debug("generated.orphan_gone", pid=pid)
    _log.warning("generated.orphans_left", left=sorted(_children() - before))


__all__ = ["RunResult", "Runner", "subprocess_runner"]
