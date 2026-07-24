"""Unit tests for promise-delivery gate helpers.

Covers :func:`_promised_test_files` in ``repoach.review.dev_runner``.
SP-PROMISE-RENAME-RETIRE removed ``_attempt_mechanical_rename`` and its
restore helper together with the mechanical-rename laundering path they
served; their unit coverage is retargeted to the fail path in
``tests/unit/test_dev_runner_promise_delivery.py``.
"""

from __future__ import annotations

from repoach.review.dev_runner import _promised_test_files


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
