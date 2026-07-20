"""Unit tests for SP-DEV-GROWTH-DELTA — size-guard polarity per context.

The ``excessive_size_delta`` placeholder layer used a symmetric cap:
growing a 63-line file to 144 lines was rejected exactly like
shrinking it — which killed legitimate Developer build steps (observed
live on the SP-DEV-PROMISE-RECONCILE round-2 dispatch). These tests
pin the new polarity: with ``allow_growth=True`` (build context) only
shrinkage fires; the Coder-loop default is byte-for-byte unchanged.
"""

from __future__ import annotations

from pathlib import Path

from repoach.review.coder_loop import apply_fixes, is_placeholder_content

_REAL_LINE = "x{} = {}\n"


def _write_module(repo_root: Path, n_lines: int) -> str:
    path = "src/grown_module.py"
    target = repo_root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(_REAL_LINE.format(i, i) for i in range(n_lines)), encoding="utf-8")
    return path


def _content(n_lines: int) -> str:
    return "".join(_REAL_LINE.format(i, i) for i in range(n_lines))


def test_growth_still_rejected_by_default(tmp_path: Path) -> None:
    path = _write_module(tmp_path, 63)
    result = is_placeholder_content(path, _content(144), repo_root=tmp_path)
    assert result.is_placeholder is True
    assert result.reason == "excessive_size_delta"


def test_growth_allowed_in_build_context(tmp_path: Path) -> None:
    path = _write_module(tmp_path, 63)
    result = is_placeholder_content(path, _content(144), repo_root=tmp_path, allow_growth=True)
    assert result.is_placeholder is False


def test_shrinkage_rejected_regardless_of_growth_flag(tmp_path: Path) -> None:
    path = _write_module(tmp_path, 100)
    for allow_growth in (False, True):
        result = is_placeholder_content(
            path, _content(40), repo_root=tmp_path, allow_growth=allow_growth
        )
        assert result.is_placeholder is True
        assert result.reason == "excessive_size_delta"


def test_massive_shrinkage_unchanged(tmp_path: Path) -> None:
    path = _write_module(tmp_path, 100)
    for allow_growth in (False, True):
        result = is_placeholder_content(
            path, "x0 = 0\n", repo_root=tmp_path, allow_growth=allow_growth
        )
        assert result.is_placeholder is True
        assert result.reason == "massive_shrinkage"


def test_apply_fixes_forwards_allow_growth(tmp_path: Path) -> None:
    path = _write_module(tmp_path, 63)
    fixes = [{"path": path, "new_content": _content(144)}]

    applied_default, rejected_default = apply_fixes(fixes, repo_root=tmp_path)
    assert applied_default == 0
    assert rejected_default == [path]

    applied_growth, rejected_growth = apply_fixes(fixes, repo_root=tmp_path, allow_growth=True)
    assert applied_growth == 1
    assert rejected_growth == []
    assert (tmp_path / path).read_text(encoding="utf-8") == _content(144)
