"""SP-NO-INLINE-COMMENTS-GATE — ratcheting CI gate.

Pins a maximum number of inline-``#`` and ``# noqa`` violations across
``src/``, ``tests/``, ``scripts/``, ``agents/``. The baseline starts at
the count measured at gate-introduction time (see
``MAX_VIOLATIONS`` below) and can only ratchet **down**: each sweep PR
removes a batch of violations and lowers the constant by the same
amount. Adding a new inline comment or ``# noqa`` therefore fails the
test until the author either removes it or lowers the baseline.

When the constant reaches ``0`` the gate becomes a hard block.

The accompanying scanner unit tests live in
``tests/unit/test_no_inline_comments_gate_scanner.py`` and exercise
the detection logic on synthetic fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repoach.lint.no_inline_comments import (
    DEFAULT_ROOTS,
    Violation,
    scan,
    summarise,
)

MAX_VIOLATIONS: int = 0
MAX_NOQA: int = 0


def _scan_repo() -> list[Violation]:
    repo_root = Path(__file__).resolve().parents[2]
    roots = [repo_root / r for r in DEFAULT_ROOTS]
    return scan(roots)


def test_violation_count_does_not_exceed_baseline() -> None:
    """The total violation count must not exceed the ratchet baseline.

    If you see this fail, either remove the new inline comment / ``noqa``
    you just introduced, or — if you have just landed a sweep PR — lower
    ``MAX_VIOLATIONS`` to match the new count.
    """
    violations = _scan_repo()
    counts = summarise(violations)
    assert counts["total"] <= MAX_VIOLATIONS, (
        f"Inline-comment / noqa count regressed: "
        f"{counts['total']} > baseline {MAX_VIOLATIONS}.\n"
        f"Per-kind: inline={counts['inline']} noqa={counts['noqa']} "
        f"files={counts['files']}.\n\n"
        "First 20 offenders:\n" + "\n".join(v.format() for v in violations[:20])
    )


def test_noqa_directive_count_does_not_exceed_baseline() -> None:
    """``# noqa`` directives ratchet down only — never up.

    Per-line lint suppression is structurally banned; suppressions
    live in ``pyproject.toml`` only. ``MAX_NOQA`` records the count
    measured at gate-introduction time and is lowered as each sweep
    PR removes a directive. When ``MAX_NOQA`` reaches ``0`` the
    gate becomes a hard zero-tolerance check.
    """
    violations = _scan_repo()
    counts = summarise(violations)
    assert counts["noqa"] <= MAX_NOQA, (
        f"# noqa count regressed: {counts['noqa']} > baseline {MAX_NOQA}. "
        "Either remove the new noqa or migrate the suppression to "
        "pyproject.toml (per-file or per-rule)."
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
