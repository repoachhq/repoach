r"""Scan for inline ``#``-comments and ``# noqa`` directives.

SP-NO-INLINE-COMMENTS-GATE — see
``docs/specs/2026-05-08_SP-NO-INLINE-COMMENTS-GATE_zero_inline_comments_and_no_noqa.md``
for rationale, scope, and roll-out plan.

The scanner walks ``.py`` files under the configured roots and
tokenises each one with the standard library ``tokenize`` module. A
``COMMENT`` token is reported as a violation when either of these
holds:

inline
    The token's source line also produced at least one non-comment,
    non-newline, non-indent, non-encoding token. In other words, code
    sits to the left of the ``#`` on that line.

noqa
    The comment body matches ``\\bnoqa\\b`` (case-insensitive),
    regardless of placement. Per-line lint suppression is forbidden;
    suppressions live in ``pyproject.toml`` only.

Implicitly allowed (because they sit on their own line and have no
``noqa`` body):

* Shebang on line 1 (``#!/usr/bin/env python3``).
* Source encoding declaration on lines 1-2
  (``# -*- coding: utf-8 -*-`` or ``# coding: utf-8``).
* Any standalone line comment, anywhere.
"""

from __future__ import annotations

import logging
import re
import token
import tokenize
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

DEFAULT_ROOTS: tuple[str, ...] = ("src", "tests", "scripts", "agents")

_NOQA_RE = re.compile(r"\bnoqa\b", re.IGNORECASE)

_NON_CODE_TOKENS: frozenset[int] = frozenset(
    {
        token.NEWLINE,
        token.NL,
        token.INDENT,
        token.DEDENT,
        token.COMMENT,
        token.ENCODING,
        token.ENDMARKER,
    }
)


@dataclass(frozen=True)
class Violation:
    r"""A single rule violation found by the scanner.

    Attributes:
        path: Source file containing the violation, relative to the
            scan root.
        line: 1-based line number of the offending ``#`` token.
        col: 1-based column of the offending ``#`` token.
        kind: Either ``"inline"`` (code sits to the left of the
            ``#``) or ``"noqa"`` (body matches ``\bnoqa\b``).
        body: Raw comment text, including the leading ``#``.
    """

    path: Path
    line: int
    col: int
    kind: str
    body: str

    def format(self) -> str:
        """Return a single ``path:line:col [kind] body`` line."""
        return f"{self.path}:{self.line}:{self.col} [{self.kind}] {self.body}"


def scan_file(path: Path) -> list[Violation]:
    """Return every violation found in a single Python source file.

    Args:
        path: Filesystem path of the file to scan.

    Returns:
        List of :class:`Violation` instances. Empty for files that
        fail to tokenise (syntax error, unsupported encoding, …) —
        such files are silently skipped because they would also
        crash any downstream tool.
    """
    try:
        with path.open("rb") as fh:
            tokens = list(tokenize.tokenize(fh.readline))
    except (tokenize.TokenError, SyntaxError, OSError, IndentationError) as exc:
        _log.debug(
            "no_inline_comments.tokenize_failed",
            extra={"path": str(path), "error_type": type(exc).__name__},
        )
        return []

    code_lines: set[int] = set()
    for tok in tokens:
        if tok.type in _NON_CODE_TOKENS:
            continue
        code_lines.add(tok.start[0])

    out: list[Violation] = []
    for tok in tokens:
        if tok.type != token.COMMENT:
            continue
        line, start_col = tok.start
        body = tok.string
        is_noqa = bool(_NOQA_RE.search(body))
        if is_noqa:
            out.append(Violation(path, line, start_col + 1, "noqa", body))
            continue
        if line in code_lines:
            out.append(Violation(path, line, start_col + 1, "inline", body))
    return out


def iter_python_files(roots: Iterable[Path]) -> Iterator[Path]:
    """Yield every ``*.py`` file under the configured roots.

    Args:
        roots: Iterable of directory paths to scan recursively.

    Yields:
        Resolved file paths ending in ``.py``. Non-existent roots
        are skipped silently.
    """
    for root in roots:
        if not root.is_dir():
            continue
        yield from sorted(root.rglob("*.py"))


def scan(roots: Iterable[Path]) -> list[Violation]:
    """Scan every Python file under ``roots`` and aggregate violations."""
    out: list[Violation] = []
    for f in iter_python_files(roots):
        out.extend(scan_file(f))
    return out


def summarise(violations: Iterable[Violation]) -> dict[str, int]:
    """Bucket violations by kind plus total file count.

    Returns:
        Dict with keys ``"inline"``, ``"noqa"``, ``"total"``, and
        ``"files"`` (number of distinct files with at least one
        violation).
    """
    inline = 0
    noqa = 0
    files: set[Path] = set()
    total = 0
    for v in violations:
        total += 1
        files.add(v.path)
        if v.kind == "noqa":
            noqa += 1
        else:
            inline += 1
    return {"inline": inline, "noqa": noqa, "total": total, "files": len(files)}
