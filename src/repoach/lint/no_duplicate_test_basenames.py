"""Scan for duplicate test-file basenames across unit and integration (SP-TEST-BASENAME-GATE).

``tests/unit/__init__.py`` and ``tests/integration/__init__.py`` make a
colliding basename (``tests/unit/test_x.py`` and
``tests/integration/test_x.py``) safely collectible under pytest's
``prepend`` import mode — each resolves to a distinct fully qualified
dotted module name (``tests.unit.test_x`` vs
``tests.integration.test_x``). This module is a *ratcheting* gate on
top of that fix: a basename shared by both directories is still a
smell (the same behavior tested twice under different names, or a file
that should live in only one tree), so a future PR that reintroduces a
new collision is caught locally before push, mirroring the house
pattern established by :mod:`repoach.lint.no_inline_comments` and
:mod:`repoach.lint.no_silent_except` (scanner module here, CLI wrapper
under ``scripts/``, pytest binding with a ``MAX_*`` baseline constant
that can only decrease).
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_UNIT_ROOT: str = "tests/unit"
DEFAULT_INTEGRATION_ROOT: str = "tests/integration"

MAX_DUPLICATES: int = 6
"""Ratchet baseline — the count measured at gate-introduction time.

The pre-commit hook and the pytest gate both enforce
``total_duplicates <= MAX_DUPLICATES``. The baseline can only ratchet
**down**: each follow-up cleanup that renames or merges one of the 6
currently-colliding pairs lowers the constant by the same amount. When
it reaches 0 the gate becomes zero-tolerance.
"""


def _test_basenames(root: Path) -> set[str]:
    """Return the basenames of every ``test_*.py`` file directly under *root*.

    Non-existent roots return an empty set rather than raising, so the
    scanner degrades gracefully when pointed at a tree that has not
    been created yet (e.g. a synthetic ``tmp_path`` fixture in a unit
    test).
    """
    if not root.is_dir():
        return set()
    return {path.name for path in root.glob("test_*.py")}


def scan(unit_root: Path, integration_root: Path) -> list[str]:
    """Return the sorted basenames of ``test_*.py`` files present in both roots.

    Args:
        unit_root: Directory scanned for the unit-suite side of the
            comparison.
        integration_root: Directory scanned for the integration-suite
            side of the comparison.

    Returns:
        Sorted list of basenames (e.g. ``["test_x.py"]``) found
        directly under both *unit_root* and *integration_root*. Empty
        when the two share no basenames, including when either root
        does not exist.
    """
    return sorted(_test_basenames(unit_root) & _test_basenames(integration_root))


def summarise(duplicates: list[str]) -> dict[str, int]:
    """Return ``{"total": len(duplicates)}`` for CLI/pytest reporting."""
    return {"total": len(duplicates)}
