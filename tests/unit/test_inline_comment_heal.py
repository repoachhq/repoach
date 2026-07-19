"""Unit tests for the inline-comment auto-heal (SP-DEV-STEP-LOOP-HARDEN)."""

from __future__ import annotations

from pathlib import Path

from repoach.review.inline_comment_heal import heal_inline_comments


def _write(root: Path, rel: str, body: str) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def test_strips_inline_comment_preserving_code(tmp_path: Path) -> None:
    target = _write(tmp_path, "src/m.py", "x = 1  # explain\ny = 2\n")

    healed = heal_inline_comments(tmp_path, ["src/m.py"])

    assert healed == ["src/m.py"]
    assert target.read_text(encoding="utf-8") == "x = 1\ny = 2\n"


def test_comment_free_file_is_untouched_and_unlisted(tmp_path: Path) -> None:
    body = '"""Module docstring."""\n\n\ndef f() -> int:\n    return 1\n'
    target = _write(tmp_path, "src/clean.py", body)

    healed = heal_inline_comments(tmp_path, ["src/clean.py"])

    assert healed == []
    assert target.read_text(encoding="utf-8") == body


def test_standalone_line_comment_is_not_healed(tmp_path: Path) -> None:
    body = "# a standalone comment\nx = 1\n"
    target = _write(tmp_path, "src/standalone.py", body)

    healed = heal_inline_comments(tmp_path, ["src/standalone.py"])

    assert healed == []
    assert target.read_text(encoding="utf-8") == body


def test_hash_inside_string_is_not_touched(tmp_path: Path) -> None:
    body = 'url = "http://x/#frag"\n'
    target = _write(tmp_path, "src/s.py", body)

    healed = heal_inline_comments(tmp_path, ["src/s.py"])

    assert healed == []
    assert target.read_text(encoding="utf-8") == body


def test_inline_comment_after_string_with_hash(tmp_path: Path) -> None:
    target = _write(tmp_path, "src/mix.py", 'u = "a#b"  # real comment\n')

    heal_inline_comments(tmp_path, ["src/mix.py"])

    assert target.read_text(encoding="utf-8") == 'u = "a#b"\n'


def test_non_python_and_missing_paths_are_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "docs/note.md", "text  # not code\n")

    healed = heal_inline_comments(tmp_path, ["docs/note.md", "src/absent.py"])

    assert healed == []


def test_multiple_files_returned_in_input_order(tmp_path: Path) -> None:
    _write(tmp_path, "src/a.py", "a = 1  # c\n")
    _write(tmp_path, "src/b.py", "b = 2\n")
    _write(tmp_path, "tests/c.py", "c = 3  # c\n")

    healed = heal_inline_comments(tmp_path, ["src/a.py", "src/b.py", "tests/c.py"])

    assert healed == ["src/a.py", "tests/c.py"]
