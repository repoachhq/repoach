"""The one way a test spawns a child ``pytest``.

A child pytest without ``--basetemp`` opens a numbered directory in the
shared ``<tmp>/pytest-of-<user>`` root. At exit, pytest deletes every
older numbered directory there that no live session locks, which are
the basetemps other suites left behind (25 000 to 48 000 entries each
on the self-hosted runner). The child's wall time then depends on what
other suites left, not on its own work. On #293, run 43314128941, that
ran a one-test child past its 60 s timeout.

:func:`child_pytest_argv` always passes a ``--basetemp`` of the
caller's choosing. ``tests/unit/test_child_pytest_basetemp.py`` refuses
any other spawn of pytest in ``tests/``.
"""

from __future__ import annotations

import sys
from pathlib import Path


def child_pytest_argv(*args: str, basetemp: Path) -> list[str]:
    """Return the argv of a child ``python -m pytest`` that owns its basetemp.

    Args:
        *args: The pytest arguments (selectors, ``-q``, ``--collect-only``, ...).
        basetemp: A directory private to this child, created by pytest
            and emptied on each run; never the shared temporary root.

    Returns:
        ``[sys.executable, "-m", "pytest", *args, "--basetemp=<basetemp>"]``,
        with ``basetemp`` made absolute so a ``cwd`` change cannot move it.

    Raises:
        ValueError: ``args`` already carries a ``--basetemp``.
    """
    if any(arg == "--basetemp" or arg.startswith("--basetemp=") for arg in args):
        raise ValueError("pass the basetemp as basetemp=, not in args")
    return [sys.executable, "-m", "pytest", *args, f"--basetemp={basetemp.resolve()}"]
