"""SP-NO-INLINE-COMMENTS-GATE — scanner detection-logic tests.

Synthetic fixtures only; the live-tree ratcheting test lives in
``tests/unit/test_no_inline_comments_gate.py``. These cases pin the
scanner's classification rules so that the gate behaves identically
across Python versions and tokenize releases.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from repoach.lint.no_inline_comments import (
    Violation,
    scan_file,
    summarise,
)


def _write(tmp_path: Path, body: str) -> Path:
    f = tmp_path / "fixture.py"
    f.write_text(body)
    return f


def test_pure_inline_comment_after_code(tmp_path: Path) -> None:
    f = _write(tmp_path, "x = 1  # this is inline\n")
    out = scan_file(f)
    assert len(out) == 1
    assert out[0].kind == "inline"
    assert out[0].line == 1


def test_standalone_line_comment_is_allowed(tmp_path: Path) -> None:
    f = _write(tmp_path, "# this is a standalone line comment\nx = 1\n")
    assert scan_file(f) == []


def test_shebang_on_line_one_is_allowed(tmp_path: Path) -> None:
    f = _write(tmp_path, "#!/usr/bin/env python3\nx = 1\n")
    assert scan_file(f) == []


def test_shebang_after_code_on_same_line_is_a_violation(tmp_path: Path) -> None:
    """Shebang allowance is strictly the line-1 prefix; once code
    sits on the same line the comment is inline and forbidden.
    """
    f = _write(tmp_path, 'cmd = "x"  #!/bin/sh fragment\n')
    out = scan_file(f)
    assert len(out) == 1
    assert out[0].kind == "inline"
    assert out[0].line == 1


def test_pep263_encoding_declaration_allowed(tmp_path: Path) -> None:
    f = _write(tmp_path, "# -*- coding: utf-8 -*-\nx = 1\n")
    assert scan_file(f) == []


def test_minimal_pep263_encoding_allowed(tmp_path: Path) -> None:
    f = _write(tmp_path, "# coding: latin-1\nx = 1\n")
    assert scan_file(f) == []


def test_noqa_on_its_own_line_is_a_violation(tmp_path: Path) -> None:
    """Standalone line comments are normally allowed, but ``noqa``
    is banned in any placement.
    """
    f = _write(tmp_path, "# noqa: E501\nx = 1\n")
    out = scan_file(f)
    assert len(out) == 1
    assert out[0].kind == "noqa"


def test_noqa_inline_is_a_noqa_violation_not_an_inline_one(
    tmp_path: Path,
) -> None:
    """When the comment is both inline and contains ``noqa``, classify
    as ``noqa`` so the operator gets the more actionable error.
    """
    f = _write(tmp_path, "x = 1  # noqa: E501\n")
    out = scan_file(f)
    assert len(out) == 1
    assert out[0].kind == "noqa"


def test_noqa_case_insensitive(tmp_path: Path) -> None:
    f = _write(tmp_path, "x = 1  # NOQA\n")
    out = scan_file(f)
    assert len(out) == 1
    assert out[0].kind == "noqa"


def test_noqa_substring_in_word_does_not_match(tmp_path: Path) -> None:
    """``\\bnoqa\\b`` only matches whole words; e.g. ``noqac`` is fine."""
    f = _write(tmp_path, "# this is not a noqacomment\nx = 1\n")
    assert scan_file(f) == []


def test_multiple_violations_in_one_file(tmp_path: Path) -> None:
    body = "x = 1  # first inline\ny = 2  # second inline\nz = 3  # noqa\n"
    f = _write(tmp_path, body)
    out = scan_file(f)
    assert len(out) == 3
    counts = summarise(out)
    assert counts == {"inline": 2, "noqa": 1, "unparseable": 0, "total": 3, "files": 1}


def test_standalone_allow_directive_permitted(tmp_path: Path) -> None:
    """The ``no_silent_except`` escape hatch is a whole-line comment; not itself flagged."""
    f = _write(
        tmp_path,
        '# repoach: allow-silent-except reason="narrow FS race, best-effort cleanup"\nx = 1\n',
    )
    assert scan_file(f) == []


def test_comment_inside_string_is_not_a_comment(tmp_path: Path) -> None:
    """``#`` inside a string literal must not trigger the scanner."""
    f = _write(tmp_path, 's = "hash # not comment"\n')
    assert scan_file(f) == []


def test_violation_format_round_trip(tmp_path: Path) -> None:
    f = _write(tmp_path, "x = 1  # message\n")
    out = scan_file(f)
    formatted = out[0].format()
    assert ":1:" in formatted
    assert "[inline]" in formatted
    assert "# message" in formatted


def test_unparseable_file_fails(tmp_path: Path) -> None:
    """Tokenize failures must not crash the gate but must FAIL it loudly:
    the file reports a single ``kind="unparseable"`` violation rather
    than being silently skipped.
    """
    f = _write(tmp_path, "def broken(:\n")
    out = scan_file(f)
    assert len(out) == 1
    assert out[0].kind == "unparseable"


def test_violation_dataclass_is_frozen() -> None:
    v = Violation(path=Path("x.py"), line=1, col=1, kind="inline", body="# x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.line = 2
