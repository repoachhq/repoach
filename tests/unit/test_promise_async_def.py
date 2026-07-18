"""Unit tests for SP-GATE-ASYNC-DEF-SELECTOR — async def recognition.

Pins that promised_present and _test_function_names_in_file recognise
``async def`` test definitions alongside ``def`` ones, with the same
word-boundary and class-tolerance semantics as the sync case.
"""

from __future__ import annotations

from pathlib import Path

from ferova.review.dev_runner import _test_function_names_in_file
from ferova.review.spec_gate import promised_present


def test_promised_present_matches_async_def(tmp_path: Path) -> None:
    """A flat ``async def test_x(`` satisfies a ``::test_x`` promise."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_async.py").write_text(
        "async def test_one():\n    assert True\n",
        encoding="utf-8",
    )
    assert promised_present(tmp_path, "tests/test_async.py::test_one") is True
    assert promised_present(tmp_path, "tests/test_async.py::test_absent") is False


def test_promised_present_async_def_class_scoped(tmp_path: Path) -> None:
    """A class-nested ``async def`` satisfies both flat and class-scoped promises."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_class_async.py").write_text(
        "class TestAsync:\n    async def test_inner(self):\n        assert True\n",
        encoding="utf-8",
    )
    assert promised_present(tmp_path, "tests/test_class_async.py::TestAsync::test_inner") is True
    assert promised_present(tmp_path, "tests/test_class_async.py::test_inner") is True
    assert promised_present(tmp_path, "tests/test_class_async.py::TestGhost::test_inner") is True


def test_promised_present_async_name_boundary(tmp_path: Path) -> None:
    """Promise ``test_foo`` is NOT satisfied by ``async def test_foobar(``."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_boundary.py").write_text(
        "async def test_foobar():\n    assert True\n",
        encoding="utf-8",
    )
    assert promised_present(tmp_path, "tests/test_boundary.py::test_foo") is False


def test_test_function_names_lists_async_defs(tmp_path: Path) -> None:
    """A mixed sync + async file lists both kinds of test functions."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mixed.py").write_text(
        "def test_sync():\n    assert True\n\nasync def test_async_one():\n    assert True\n\nasync def test_async_two():\n    assert True\n",
        encoding="utf-8",
    )
    names = _test_function_names_in_file(tmp_path, "tests/test_mixed.py")
    assert names == ["test_async_one", "test_async_two", "test_sync"]


def test_async_only_test_file_not_placeholder(tmp_path: Path) -> None:
    """A test file with only ``async def test_*`` is not a placeholder."""
    from ferova.review.coder_loop import is_placeholder_content

    result = is_placeholder_content(
        "tests/test_async_only.py",
        "async def test_one():\n    assert True\n\nasync def test_two():\n    assert True\n",
        repo_root=tmp_path,
    )
    assert result.is_placeholder is False
