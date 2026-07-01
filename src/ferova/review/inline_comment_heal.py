"""Deterministic inline-comment auto-heal for the Developer step loop (SP-DEV-STEP-LOOP-HARDEN).

The autonomous Developer's weaker coder tails frequently emit ``# ...``
trailing comments, which trip the repo's no-inline-comments golden rule
(SP-NO-INLINE-COMMENTS-GATE) and fail a step that is otherwise correct. Rather
than spend a retry hoping the model complies, the step loop heals the slip
mechanically: it reuses the gate's own tokenizer
(:func:`ferova.lint.no_inline_comments.scan_file`) to locate every inline
comment and strips it from the ``#`` to end-of-line, leaving the code to its
left and the file's other lines untouched.

Only ``kind == "inline"`` violations are healed — code sits to the left of the
``#``, so cutting at the comment column never removes code. ``noqa`` and
standalone full-line comments are deliberately left for the gate to reject:
removing a whole line is not safe to automate.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from ferova.core.logging import get_logger
from ferova.lint.no_inline_comments import scan_file

_log = get_logger(__name__)

__all__ = ["heal_inline_comments"]


def _strip_line(raw: str, cut_col: int) -> str:
    """Return ``raw`` with the inline comment at ``cut_col`` removed.

    Args:
        raw: The original source line, including its end-of-line characters.
        cut_col: 0-based column of the ``#`` that opens the inline comment.

    Returns:
        The code before ``cut_col`` (trailing whitespace stripped) followed by
        the line's original end-of-line characters.
    """
    body = raw.rstrip("\r\n")
    eol = raw[len(body) :]
    return body[:cut_col].rstrip() + eol


def heal_inline_comments(repo_root: Path, paths: Iterable[str]) -> list[str]:
    """Strip inline ``#`` comments from the given Python files in place.

    Args:
        repo_root: Repository root the paths are relative to.
        paths: Repo-relative file paths to heal; non-``.py`` paths and files
            absent on disk are skipped.

    Returns:
        The repo-relative paths that were modified, in input order.
    """
    healed: list[str] = []
    for rel in paths:
        target = repo_root / rel
        if target.suffix != ".py" or not target.is_file():
            continue
        cut_by_line: dict[int, int] = {}
        for violation in scan_file(target):
            if violation.kind != "inline":
                continue
            col0 = violation.col - 1
            cut_by_line[violation.line] = min(cut_by_line.get(violation.line, col0), col0)
        if not cut_by_line:
            continue
        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        for line_no, cut_col in cut_by_line.items():
            index = line_no - 1
            if 0 <= index < len(lines):
                lines[index] = _strip_line(lines[index], cut_col)
        target.write_text("".join(lines), encoding="utf-8")
        healed.append(rel)
        _log.info("inline_comment_heal.healed", path=rel, lines=sorted(cut_by_line))
    return healed
