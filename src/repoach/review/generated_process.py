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
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..core.logging import get_logger
from .generated_types import GeneratedEntry, GeneratedState, declared_argv, plain_file, tail_lines

_log = get_logger(__name__)

OUTPUT_READ_MAX_BYTES = 64 * 1024
"""Bytes read back from the end of a check's output: only its tail is ever kept."""


@dataclass(frozen=True)
class RunResult:
    """What a :class:`Runner` returns: the exit code and the merged output."""

    exit_code: int
    output: str


INTERPRETERS: dict[str, str] = {".py": "python3", ".sh": "bash"}
"""The interpreter a check script runs under, by its extension (G1).

Any other script must carry the executable bit and runs as itself."""


def interpreted(argv: Sequence[str], head_dir: Path) -> list[str]:
    """Return the command line of a check: its interpreter, then the declared argv.

    Args:
        argv: The declared check, placeholders replaced.
        head_dir: The tree the check runs in.

    Returns:
        ``[interpreter, *argv]`` for a script :data:`INTERPRETERS` names,
        else the script's path in ``head_dir`` followed by its arguments.
    """
    script = argv[0]
    interpreter = INTERPRETERS.get(Path(script).suffix)
    if interpreter is not None:
        return [interpreter, *argv]
    return [str(head_dir / script), *argv[1:]]


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
        shell: bool = False,
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


def orphan_sweep_available() -> bool:
    """Whether this platform can adopt and sweep a check's detached descendants (Linux only).

    Elsewhere the check's session is still killed whole; only a
    descendant that left the session can outlive the run, and
    :func:`subprocess_runner` says so once per process.

    Returns:
        True on Linux, where ``PR_SET_CHILD_SUBREAPER`` and ``/proc`` exist.
    """
    return sys.platform.startswith("linux") and Path("/proc").is_dir()


def _adopt_orphans() -> bool:
    """Make this process the subreaper of its descendants; False where the kernel cannot.

    A process the check detaches from its session (``setsid``, a double
    fork) escapes :func:`_kill_group`; as a subreaper, this process
    adopts it when its parent dies, and :func:`_kill_orphans` kills it.
    """
    if not orphan_sweep_available():
        _log.warning(
            "generated.orphan_sweep_unavailable",
            platform=sys.platform,
            consequence="a descendant that leaves the check's session can outlive it",
        )
        return False
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


class ScriptTamperedError(RuntimeError):
    """The check script became a symlink, or left the head tree, while the base script ran."""


@contextmanager
def script_at(head_dir: Path, script: str, content: bytes | None) -> Iterator[None]:
    """Put ``content`` at ``script`` in the head tree for one run, then restore it (D3).

    The run is pull-request code: by the time it returns every process it
    started is dead, but it may have turned ``script`` (or a directory on
    its way) into a symlink. The head script is then not written back
    through it.

    Raises:
        ScriptTamperedError: ``script`` is no longer a plain file of the tree.
    """
    if content is None:
        yield
        return
    target = head_dir / script
    original = target.read_bytes()
    mode = target.stat().st_mode
    target.write_bytes(content)
    try:
        yield
    finally:
        if not plain_file(head_dir, script):
            raise ScriptTamperedError(
                f"check script {script} changed into a symlink during the run"
            )
        target.write_bytes(original)
        target.chmod(mode)


@dataclass
class Budget:
    """The PR's check budget, measured on ``clock``."""

    total_s: float
    clock: Callable[[], float]
    started: float = field(init=False)

    def __post_init__(self) -> None:
        """Start the budget now."""
        self.started = self.clock()

    def remaining(self) -> float:
        """Seconds left before the budget is spent."""
        return self.total_s - (self.clock() - self.started)


def run_once(
    entry: GeneratedEntry,
    path: str,
    *,
    head_dir: Path,
    head_sha: str,
    base_sha: str,
    runner: Runner,
    timeout_s: int,
    budget: Budget,
    script_content: bytes | None,
    env: Mapping[str, str],
) -> tuple[GeneratedState, int | None, int, str]:
    """Run one check once and say how it ended.

    Args:
        entry: The artefact's declaration.
        path: The artefact's path.
        head_dir: The tree the run uses.
        head_sha: The head (``{head}``).
        base_sha: The base (``{base}``).
        runner: The subprocess seam.
        timeout_s: Seconds one run may take, at most.
        budget: What is left of the pull request's budget.
        script_content: The base script to put in place for this run
            (D3), ``None`` for the head script.
        env: The scrubbed environment.

    Returns:
        ``(state, exit code, ms, output tail)``: ``skipped (cost)`` when the
        budget is spent or the run outlived its limit, ``stale`` for a
        symlinked or tampered script, an exception or a non-zero exit,
        ``verified`` for exit 0 (the caller then checks sources and the
        artefact's blob).
    """
    limit = min(entry.check_timeout_s, timeout_s)
    remaining = budget.remaining()
    if remaining <= 0:
        return (
            GeneratedState.SKIPPED_COST,
            None,
            0,
            f"check not run within {budget.total_s:g} s: {path}",
        )
    allowed = min(limit, remaining)
    if not plain_file(head_dir, entry.check[0]):
        return (
            GeneratedState.STALE,
            None,
            0,
            f"check script {entry.check[0]} is a symlink or leaves the head tree",
        )
    argv = interpreted(declared_argv(entry, head_sha, base_sha), head_dir)
    started = budget.clock()
    try:
        with script_at(head_dir, entry.check[0], script_content):
            result = runner(argv, cwd=head_dir, env=env, timeout_s=allowed, shell=False)
    except subprocess.TimeoutExpired:
        ms = round((budget.clock() - started) * 1000)
        return GeneratedState.SKIPPED_COST, None, ms, f"check not run within {allowed:g} s: {path}"
    except ScriptTamperedError as exc:
        ms = round((budget.clock() - started) * 1000)
        _log.warning("generated.check_script_tampered", path=path)
        return GeneratedState.STALE, None, ms, str(exc)
    except Exception as exc:
        ms = round((budget.clock() - started) * 1000)
        _log.warning("generated.check_raised", path=path, exc=type(exc).__name__)
        return GeneratedState.STALE, None, ms, type(exc).__name__
    elapsed = budget.clock() - started
    ms = round(elapsed * 1000)
    if elapsed > allowed:
        return GeneratedState.SKIPPED_COST, None, ms, f"check not run within {allowed:g} s: {path}"
    state = GeneratedState.VERIFIED if result.exit_code == 0 else GeneratedState.STALE
    return state, result.exit_code, ms, tail_lines(result.output)


__all__ = [
    "INTERPRETERS",
    "Budget",
    "RunResult",
    "Runner",
    "ScriptTamperedError",
    "interpreted",
    "orphan_sweep_available",
    "run_once",
    "script_at",
    "subprocess_runner",
]
