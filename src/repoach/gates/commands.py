"""Run the project's own lint, test and gate commands (SP-UNIVERSAL-GATES G1).

A command string from ``[commands]`` is split with :mod:`shlex` (never
a shell), leading ``NAME=value`` words become environment overrides
(``YOOB_CI_WAIT=1 ./ci/run-local-ci.sh --affected``), and the command
runs in the project root with a secret-scrubbed environment and a
timeout.  Every runner returns ``(passed, tail)``: ``tail`` is the
command line followed by the last :data:`TAIL_CHARS` characters of the
combined output, so a red gate always says what ran and how it failed.
A missing binary or a timeout is a red gate with a one-line reason,
never a traceback.
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..core.logging import get_logger
from .config import GatesConfig

_log = get_logger(__name__)

TAIL_CHARS = 4000
"""Characters of combined output kept in a gate's tail."""

_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
"""A leading ``NAME=value`` word of a command line."""


@dataclass(frozen=True)
class CommandResult:
    """One finished (or refused) command.

    Attributes:
        passed: Exit status 0.
        returncode: The exit status; ``None`` when it never ran or timed out.
        tail: Command line plus the last :data:`TAIL_CHARS` of output.
        stdout: The full standard output (empty when it never ran).
    """

    passed: bool
    returncode: int | None
    tail: str
    stdout: str = ""


def _scrubbed_env() -> dict[str, str]:
    """Return the secret-scrubbed environment for project commands.

    Imported here rather than at module level: :mod:`repoach.review`
    imports this package, and importing ``repoach.review.secret_env`` at
    module import time would initialise the whole ``review`` package
    first and close an import cycle.
    """
    from ..review.secret_env import scrubbed_env

    return scrubbed_env()


def split_command(command: str) -> tuple[dict[str, str], list[str]]:
    """Split a command string into environment overrides and argv.

    Args:
        command: A ``[commands]`` value.

    Returns:
        ``(env_overrides, argv)``; ``argv`` is empty when the string
        holds only assignments or nothing.

    Raises:
        ValueError: The string is not valid shell quoting.
    """
    words = shlex.split(command)
    overrides: dict[str, str] = {}
    while words and _ASSIGNMENT_RE.match(words[0]):
        name, _, value = words.pop(0).partition("=")
        overrides[name] = value
    return overrides, words


def run_argv(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_s: int,
    env_overrides: Mapping[str, str] | None = None,
    label: str | None = None,
) -> CommandResult:
    """Run ``argv`` in ``cwd`` with the scrubbed environment and a timeout.

    Args:
        argv: The command and its arguments.
        cwd: Working directory.
        timeout_s: Seconds before the process is killed.
        env_overrides: Variables layered over the scrubbed environment.
        label: Command line shown at the head of the tail (defaults to
            the quoted ``argv``).

    Returns:
        The :class:`CommandResult`.
    """
    shown = label if label is not None else shlex.join(argv)
    env = _scrubbed_env()
    if env_overrides:
        env.update(env_overrides)
    try:
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
    except FileNotFoundError:
        _log.warning("gates.command_not_found", command=shown)
        return CommandResult(False, None, f"$ {shown}\ncommand not found: {argv[0]}")
    except PermissionError as exc:
        _log.warning("gates.command_not_executable", command=shown, error=str(exc)[:200])
        return CommandResult(False, None, f"$ {shown}\ncommand not executable: {argv[0]}")
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        _log.warning("gates.command_timeout", command=shown, timeout_s=timeout_s)
        _kill_group(proc)
        stdout, stderr = proc.communicate()
        partial = _decode(exc.stdout) + _decode(stdout) + _decode(stderr)
        return CommandResult(
            False, None, f"$ {shown}\n{partial[-TAIL_CHARS:]}\ntimeout after {timeout_s}s"
        )
    output = (stdout or "") + (stderr or "")
    tail = f"$ {shown}\n{output[-TAIL_CHARS:]}"
    if proc.returncode != 0:
        tail += f"\nexit status {proc.returncode}"
    return CommandResult(proc.returncode == 0, proc.returncode, tail, stdout or "")


def _kill_group(proc: subprocess.Popen[str]) -> None:
    """Kill the whole process group of a timed-out command, children included.

    The command runs in its own session (``start_new_session=True``), so
    its process group id is its pid; killing the group takes down the
    background children a script spawned, which a plain ``proc.kill()``
    would orphan with the output pipes still open.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError as exc:
        _log.info("gates.command_group_gone", pid=proc.pid, error=str(exc)[:200])


def _decode(raw: bytes | str | None) -> str:
    """Return captured output as text whatever form the timeout carried it in."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return raw


def run_command(command: str, *, root: Path, timeout_s: int) -> CommandResult:
    """Run one ``[commands]`` string in ``root``.

    Args:
        command: The configured command line.
        root: The project root (working directory).
        timeout_s: Seconds before the process is killed.

    Returns:
        The :class:`CommandResult`; a malformed or assignment-only
        string is a red result naming the problem.
    """
    try:
        overrides, argv = split_command(command)
    except ValueError as exc:
        return CommandResult(False, None, f"$ {command}\nunparseable command: {exc}")
    if not argv:
        return CommandResult(False, None, f"$ {command}\nno executable in command")
    if "/" in argv[0] and not os.path.isabs(argv[0]):
        argv[0] = str((root / argv[0]).resolve())
    return run_argv(argv, cwd=root, timeout_s=timeout_s, env_overrides=overrides, label=command)


def _skipped(name: str) -> tuple[bool, str]:
    """Return the green, printed skip of an unconfigured command."""
    return True, f"skipped: no [commands].{name} configured"


def run_lint_gate(root: Path, config: GatesConfig) -> tuple[bool, str]:
    """Run ``[commands].lint`` in ``root``.

    Args:
        root: The project root.
        config: The project configuration.

    Returns:
        ``(passed, tail)``; ``(True, "skipped: …")`` when no lint
        command is configured.
    """
    if not config.lint_cmd:
        return _skipped("lint")
    result = run_command(config.lint_cmd, root=root, timeout_s=config.timeout_s)
    return result.passed, result.tail


def expand_test_command(
    command: str, *, target: str | None = None, python: str | None = None
) -> str:
    """Expand ``{python}`` and ``{target}`` in a test command.

    ``{python}`` becomes the given interpreter (``python`` when ``None``);
    ``{target}`` becomes the quoted target, and when the command has no
    ``{target}`` placeholder a non-empty target is appended
    (SP-UNIVERSAL-GATES A2).

    Args:
        command: ``[commands].test``.
        target: Optional repo-relative test target.
        python: Optional interpreter (``REPOACH_CODER_PYTHONS`` entry).

    Returns:
        The expanded command line.
    """
    expanded = command.replace("{python}", shlex.quote(python or "python"))
    quoted = shlex.quote(target) if target else ""
    if "{target}" in expanded:
        return expanded.replace("{target}", quoted).strip()
    if target:
        return f"{expanded} {quoted}"
    return expanded


def run_test_gate(
    root: Path,
    config: GatesConfig,
    *,
    target: str | None = None,
    python: str | None = None,
) -> tuple[bool, str]:
    """Run ``[commands].test`` in ``root``.

    Args:
        root: The project root.
        config: The project configuration.
        target: Optional test target passed to the command.
        python: Interpreter substituted for ``{python}``.

    Returns:
        ``(passed, tail)``; ``(True, "skipped: …")`` when no test
        command is configured.

    Raises:
        ValueError: ``target`` starts with ``-`` (it would act as a flag).
    """
    if not config.test_cmd:
        return _skipped("test")
    if target is not None and target.lstrip().startswith("-"):
        raise ValueError(f"test target must not start with '-': {target!r}")
    command = expand_test_command(config.test_cmd, target=target, python=python)
    result = run_command(command, root=root, timeout_s=config.timeout_s)
    return result.passed, result.tail


def run_project_gate(root: Path, config: GatesConfig) -> tuple[bool, str]:
    """Run ``[commands].gate`` (the long-running project barrier) in ``root``.

    Args:
        root: The project root.
        config: The project configuration.

    Returns:
        ``(passed, tail)``; ``(True, "skipped: …")`` when no gate
        command is configured.
    """
    if not config.gate_cmd:
        return _skipped("gate")
    result = run_command(config.gate_cmd, root=root, timeout_s=config.timeout_s)
    return result.passed, result.tail


__all__ = [
    "TAIL_CHARS",
    "CommandResult",
    "expand_test_command",
    "run_argv",
    "run_command",
    "run_lint_gate",
    "run_project_gate",
    "run_test_gate",
    "split_command",
]
