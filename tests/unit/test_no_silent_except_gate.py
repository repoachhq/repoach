"""SP-LINT-LOG-CATCH-ALL — ratcheting CI gate.

Pins a maximum number of silent-``except`` handlers across ``src/``,
``tests/``, ``scripts/``, ``agents/``. The baseline starts at the
count measured at gate-introduction time (see
:data:`~repoach.lint.no_silent_except.MAX_SILENT_EXCEPT`) and can
only ratchet **down**: each follow-up logging spec
(SP-COLLECTORS-LOGGING, ...) removes a batch of
silent excepts and lowers the constant by the same amount. Adding a
new silent ``except`` therefore fails the test until the author
either logs the failure or lowers the baseline.

When the constant reaches ``0`` the gate becomes a hard block — the
same end-state as SP-NO-INLINE-COMMENTS-GATE.

The accompanying scanner unit tests live in
``tests/unit/test_no_silent_except_scanner.py`` and exercise the
detection logic on synthetic fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repoach.lint.no_silent_except import (
    DEFAULT_ROOTS,
    MAX_SILENT_EXCEPT,
    SilentExceptViolation,
    scan,
    summarise,
)


def _scan_repo() -> list[SilentExceptViolation]:
    repo_root = Path(__file__).resolve().parents[2]
    roots = [repo_root / r for r in DEFAULT_ROOTS]
    return scan(roots)


def test_silent_except_count_does_not_exceed_baseline() -> None:
    """The total silent-except count must not exceed the ratchet baseline.

    If you see this fail, either log the new ``except`` you just
    introduced — ``_log.warning("event_name", reason=str(exc), ...)``
    before the swallow — or, if you have just landed a logging
    clean-up spec, lower ``MAX_SILENT_EXCEPT`` to match the new
    count.
    """
    violations = _scan_repo()
    counts = summarise(violations)
    assert counts["total"] <= MAX_SILENT_EXCEPT, (
        f"Silent-except count regressed: "
        f"{counts['total']} > baseline {MAX_SILENT_EXCEPT}.\n"
        f"Per-kind: pass={counts['pass']} return={counts['return']} "
        f"continue={counts['continue']} files={counts['files']}.\n\n"
        "First 20 offenders:\n" + "\n".join(v.format() for v in violations[:20])
    )


@pytest.mark.parametrize("root_name", DEFAULT_ROOTS)
def test_scan_root_resolves(root_name: str) -> None:
    """Every default root either exists or is skipped silently.

    Belt-and-braces: the scanner tolerates missing roots, but the
    repo's expected layout has all four. If a directory disappears,
    spec out the rename rather than letting the gate silently lose
    coverage.
    """
    repo_root = Path(__file__).resolve().parents[2]
    expected = {"src", "tests", "scripts", "agents"}
    assert root_name in expected
    target = repo_root / root_name
    if root_name in {"src", "tests", "scripts"}:
        assert target.is_dir(), f"Expected scan root missing: {target}"
