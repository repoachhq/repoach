"""Unit tests for promise-delivery gate helpers.

Covers :func:`_promised_test_files` and
:func:`_attempt_mechanical_rename` in ``repoach.review.dev_runner``.
"""

from __future__ import annotations

from pathlib import Path

from repoach.review.dev_runner import _attempt_mechanical_rename, _promised_test_files


class TestPromisedTestFiles:
    """Tests for :func:`_promised_test_files`."""

    def test_promised_test_files_extracts_paths(self) -> None:
        """Every selector with ``::`` yields its file portion."""
        selectors = [
            "tests/unit/test_a.py::test_one",
            "tests/unit/test_a.py::test_two",
            "tests/unit/test_b.py::test_three",
        ]
        result = _promised_test_files(selectors)
        assert result == {"tests/unit/test_a.py", "tests/unit/test_b.py"}

    def test_promised_test_files_skips_bare_file_selectors(self) -> None:
        """Selectors without ``::`` are excluded from the returned set."""
        selectors = [
            "tests/unit/test_a.py",
            "tests/unit/test_b.py::test_four",
        ]
        result = _promised_test_files(selectors)
        assert result == {"tests/unit/test_b.py"}

    def test_promised_test_files_empty_list(self) -> None:
        """An empty promised list returns an empty set."""
        assert _promised_test_files([]) == set()


class TestAttemptMechanicalRename:
    """Tests for :func:`_attempt_mechanical_rename`."""

    def test_attempt_mechanical_rename_single_drift_renames(self, tmp_path: Path) -> None:
        """Exactly one missing promised + one unpromised candidate renames."""
        test_file = tmp_path / "test_example.py"
        test_file.write_text(
            "def test_delivered_name():\n    assert True\n",
            encoding="utf-8",
        )
        outcome, original = _attempt_mechanical_rename(
            tmp_path,
            "test_example.py",
            promised=["test_promised_name"],
            delivered=["test_delivered_name"],
        )
        assert outcome == "renamed"
        assert "def test_delivered_name(" in original
        content = test_file.read_text(encoding="utf-8")
        assert "def test_promised_name(" in content
        assert "def test_delivered_name(" not in content

    def test_attempt_mechanical_rename_ambiguous_missing_no_rename(self, tmp_path: Path) -> None:
        """Two missing promised names — no rename, file untouched."""
        test_file = tmp_path / "test_example.py"
        seeded = "def test_delivered_name():\n    assert True\n"
        test_file.write_text(seeded, encoding="utf-8")
        outcome, original = _attempt_mechanical_rename(
            tmp_path,
            "test_example.py",
            promised=["test_promised_a", "test_promised_b"],
            delivered=["test_delivered_name"],
        )
        assert outcome == "ambiguous"
        assert original == ""
        assert test_file.read_text(encoding="utf-8") == seeded

    def test_attempt_mechanical_rename_ambiguous_candidates_no_rename(self, tmp_path: Path) -> None:
        """Two unpromised candidate functions — no rename, file untouched."""
        test_file = tmp_path / "test_example.py"
        seeded = "def test_extra_a():\n    assert True\n\ndef test_extra_b():\n    assert True\n"
        test_file.write_text(seeded, encoding="utf-8")
        outcome, original = _attempt_mechanical_rename(
            tmp_path,
            "test_example.py",
            promised=["test_promised_name"],
            delivered=["test_extra_a", "test_extra_b"],
        )
        assert outcome == "ambiguous"
        assert original == ""
        assert test_file.read_text(encoding="utf-8") == seeded

    def test_attempt_mechanical_rename_parse_error_restores(self, tmp_path: Path) -> None:
        """A rename that would produce invalid syntax reports error, file intact."""
        test_file = tmp_path / "test_example.py"
        seeded = "def test_delivered_name():\n    return (\n"
        test_file.write_text(seeded, encoding="utf-8")
        outcome, original = _attempt_mechanical_rename(
            tmp_path,
            "test_example.py",
            promised=["test_promised_name"],
            delivered=["test_delivered_name"],
        )
        assert outcome == "error"
        assert original == ""
        assert test_file.read_text(encoding="utf-8") == "def test_delivered_name():\n    return (\n"

    def test_attempt_mechanical_rename_nonexistent_file_returns_false(self, tmp_path: Path) -> None:
        """A missing file reports the error outcome without raising."""
        outcome, original = _attempt_mechanical_rename(
            tmp_path,
            "nonexistent.py",
            promised=["test_promised_name"],
            delivered=["test_delivered_name"],
        )
        assert outcome == "error"
        assert original == ""
