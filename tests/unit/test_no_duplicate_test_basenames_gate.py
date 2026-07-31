"""SP-TEST-BASENAME-GATE — ratcheting lint for duplicate test basenames.

Pins a maximum number of ``test_*.py`` basenames shared by
``tests/unit/`` and ``tests/integration/``. The baseline starts at the
count measured at gate-introduction time
(:data:`~repoach.lint.no_duplicate_test_basenames.MAX_DUPLICATES`) and
can only ratchet **down**: renaming or merging one of the 6
currently-colliding pairs lowers the constant by the same amount.
Adding a new colliding basename fails the test until the author either
renames the file or lowers the baseline — mirroring
``test_no_silent_except_gate.py`` and ``test_no_inline_comments_gate.py``.

The scanner unit tests below exercise the detection logic on synthetic
``tmp_path`` fixtures, independent of the real repo tree.
"""

from __future__ import annotations

from pathlib import Path

from repoach.lint.no_duplicate_test_basenames import (
    DEFAULT_INTEGRATION_ROOT,
    DEFAULT_UNIT_ROOT,
    MAX_DUPLICATES,
    scan,
)


def test_scan_detects_synthetic_duplicate(tmp_path: Path) -> None:
    """A basename present in both temporary directories is reported."""
    unit_root = tmp_path / "unit"
    integration_root = tmp_path / "integration"
    unit_root.mkdir()
    integration_root.mkdir()
    (unit_root / "test_synthetic_dup.py").write_text("", encoding="utf-8")
    (integration_root / "test_synthetic_dup.py").write_text("", encoding="utf-8")

    duplicates = scan(unit_root, integration_root)

    assert duplicates == ["test_synthetic_dup.py"]


def test_scan_returns_empty_for_disjoint_basenames(tmp_path: Path) -> None:
    """Two temporary directories with no shared basenames report no duplicates."""
    unit_root = tmp_path / "unit"
    integration_root = tmp_path / "integration"
    unit_root.mkdir()
    integration_root.mkdir()
    (unit_root / "test_only_in_unit.py").write_text("", encoding="utf-8")
    (integration_root / "test_only_in_integration.py").write_text("", encoding="utf-8")

    duplicates = scan(unit_root, integration_root)

    assert duplicates == []


def test_duplicate_basename_count_does_not_exceed_baseline() -> None:
    """The real repo tree's duplicate count must not exceed the ratchet baseline.

    If you see this fail, either rename the newly-colliding test file
    to a unique basename, or, if you have just landed a rename that
    resolves one of the 6 existing pairs, lower ``MAX_DUPLICATES`` to
    match the new count.
    """
    repo_root = Path(__file__).resolve().parents[2]
    duplicates = scan(repo_root / DEFAULT_UNIT_ROOT, repo_root / DEFAULT_INTEGRATION_ROOT)
    assert len(duplicates) <= MAX_DUPLICATES, (
        f"Duplicate test basename count regressed: {len(duplicates)} > "
        f"baseline {MAX_DUPLICATES}.\nOffenders: {duplicates}"
    )
