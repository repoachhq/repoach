"""Unit tests for SP-DEV-TARGETED-PATCH — anchored edits end to end.

Pins the apply semantics (ordered, exact-match, unique anchor), the
directive failure reports a retry can act on, the fix-normaliser's
acceptance of the ``edits`` shape, and the ``apply_fixes`` integration
(materialise → guards → write, with failures surfaced through
``edit_failures_out``).
"""

from __future__ import annotations

from pathlib import Path

from ferova.review.coder_loop import apply_fixes
from ferova.review.patch_apply import apply_search_replace_edits
from ferova.review.reviewer import _normalise_fixes


def test_single_edit_applies() -> None:
    out, report = apply_search_replace_edits(
        "alpha\nbeta\ngamma\n", [{"search": "beta", "replace": "BETA"}]
    )
    assert report == ""
    assert out == "alpha\nBETA\ngamma\n"


def test_edits_apply_in_order() -> None:
    out, _report = apply_search_replace_edits(
        "x = 1\n",
        [
            {"search": "x = 1", "replace": "x = 2"},
            {"search": "x = 2", "replace": "x = 3"},
        ],
    )
    assert out == "x = 3\n"


def test_anchor_not_found_is_directive() -> None:
    out, report = apply_search_replace_edits(
        "def real_function():\n    pass\n",
        [{"search": "def imaginary_function():", "replace": "x"}],
    )
    assert out is None
    assert "not found" in report
    assert "real_function" in report


def test_ambiguous_anchor_reports_count() -> None:
    out, report = apply_search_replace_edits("dup\ndup\n", [{"search": "dup", "replace": "x"}])
    assert out is None
    assert "matches 2 times" in report
    assert "unique" in report


def test_invalid_edit_shapes_rejected() -> None:
    assert apply_search_replace_edits("a", [{"search": "", "replace": "b"}])[0] is None
    assert apply_search_replace_edits("a", [{"replace": "b"}])[0] is None
    assert apply_search_replace_edits("a", ["not a dict"])[0] is None


def test_normalise_fixes_accepts_edits_shape() -> None:
    fixes = _normalise_fixes(
        [
            {"path": "src/x.py", "edits": [{"search": "a", "replace": "b"}]},
            {"path": "src/y.py", "new_content": "print(1)\n"},
            {"path": "src/z.py", "edits": [{"search": "", "replace": "b"}]},
            {"path": "src/w.py"},
        ]
    )
    assert [f["path"] for f in fixes] == ["src/x.py", "src/y.py"]
    assert fixes[0]["edits"] == [{"search": "a", "replace": "b"}]
    assert "new_content" in fixes[1]


def test_apply_fixes_materialises_edits(tmp_path: Path) -> None:
    target = tmp_path / "src" / "mod.py"
    target.parent.mkdir(parents=True)
    target.write_text("value = 1\nrest = 2\n", encoding="utf-8")

    applied, rejected = apply_fixes(
        [{"path": "src/mod.py", "edits": [{"search": "value = 1", "replace": "value = 9"}]}],
        repo_root=tmp_path,
        allow_growth=True,
    )
    assert applied == 1
    assert rejected == []
    assert target.read_text(encoding="utf-8") == "value = 9\nrest = 2\n"


def test_apply_fixes_edits_on_missing_file_rejected(tmp_path: Path) -> None:
    failures: list[str] = []
    applied, rejected = apply_fixes(
        [{"path": "src/ghost.py", "edits": [{"search": "a", "replace": "b"}]}],
        repo_root=tmp_path,
        edit_failures_out=failures,
    )
    assert applied == 0
    assert rejected == ["src/ghost.py"]
    assert failures and "does not exist" in failures[0]


def test_apply_fixes_failed_anchor_surfaces_report(tmp_path: Path) -> None:
    target = tmp_path / "src" / "mod.py"
    target.parent.mkdir(parents=True)
    target.write_text("actual = 1\n", encoding="utf-8")

    failures: list[str] = []
    applied, rejected = apply_fixes(
        [{"path": "src/mod.py", "edits": [{"search": "phantom = 0", "replace": "x"}]}],
        repo_root=tmp_path,
        edit_failures_out=failures,
    )
    assert applied == 0
    assert rejected == ["src/mod.py"]
    assert failures and "not found" in failures[0]
