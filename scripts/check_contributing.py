#!/usr/bin/env python3
"""Check that CONTRIBUTING.md cites only paths and scripts that exist (SP-CONTRIBUTING).

``CONTRIBUTING.md`` is a governed spec's artifact, not a wiki page:
every command and path it names has to keep existing. ``check_links.py``
deliberately skips fenced code blocks and inline code spans ("a URL
shown as an example is not a link"), which is exactly where
``CONTRIBUTING.md`` cites repository-relative commands
(``scripts/ci_local.sh``, ``pytest -q tests/unit``), so this is a
narrower, separate checker rather than a mode of that one.

A token is a candidate only when its first path segment (or its whole
name, for a bare top-level file) names a real top-level entry of the
repository — this is what tells ``scripts/ci_local.sh`` (a real path)
apart from ``origin/develop`` or ``feat/my-change`` (a git ref that
happens to contain a slash, never a candidate because ``origin`` and
``feat`` are not top-level entries). A token carrying a template
placeholder (``<SP-ID>``, an ellipsis) is never a candidate either —
it names a shape, not a path. The repository root is resolved with
``git rev-parse --show-toplevel`` rather than assumed from this
file's own location, so the check runs correctly from a worktree too.

Usage::

    python scripts/check_contributing.py CONTRIBUTING.md

Exit codes:

* ``0`` every candidate path resolves; the last line is
  ``contributing-paths: N checked, 0 missing``.
* ``1`` at least one candidate does not exist, one
  ``<file>:<line> <token> missing`` line each.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_TRAILING_PUNCTUATION = ".,;:)]}\"'`"
_LEADING_PUNCTUATION = "([{\"'`"
_IGNORED_PREFIXES = ("-", "$", "<", "https://", "http://", "mailto:")

EXIT_OK = 0
EXIT_MISSING = 1


@dataclass(frozen=True)
class Citation:
    """One candidate path found in a Markdown file.

    Attributes:
        file: The Markdown file that names it.
        line: 1-based line number of the occurrence.
        token: The candidate path, as written (punctuation stripped).
    """

    file: Path
    line: int
    token: str


def repo_root() -> Path:
    """Return the repository root, resolved through git rather than assumed."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip())


def top_level_entries(root: Path) -> frozenset[str]:
    """Return the names of every entry directly under *root*, hidden ones included."""
    return frozenset(entry.name for entry in root.iterdir())


def _strip_punctuation(token: str) -> str:
    """Trim the leading and trailing punctuation a sentence puts around a path."""
    return token.strip(_LEADING_PUNCTUATION + _TRAILING_PUNCTUATION)


def _is_candidate(token: str, entries: frozenset[str]) -> bool:
    """Whether *token* names a repository path candidate, judged by its first segment."""
    if not token or token.startswith(_IGNORED_PREFIXES):
        return False
    if "://" in token or " " in token:
        return False
    if any(marker in token for marker in ("<", ">", "…")):
        return False
    first_segment = token.split("/")[0]
    return first_segment in entries


def _candidates_in_text(text: str) -> list[str]:
    """Split inline-code or fenced-code text into whitespace-separated tokens."""
    return [_strip_punctuation(word) for word in text.split()]


def extract_citations(path: Path, entries: frozenset[str]) -> list[Citation]:
    """Read every path-shaped candidate of a Markdown file, in file order.

    Args:
        path: The Markdown file.
        entries: The repository's top-level entry names.

    Returns:
        One :class:`Citation` per candidate token, in file order.
    """
    citations: list[Citation] = []
    in_fence = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        texts = [line] if in_fence else [m.group(1) for m in _INLINE_CODE.finditer(line)]
        for text in texts:
            for word in _candidates_in_text(text):
                if _is_candidate(word, entries):
                    citations.append(Citation(file=path, line=number, token=word))
    return citations


def missing_citations(citations: Sequence[Citation], root: Path) -> list[Citation]:
    """Return the citations whose token does not exist on disk, relative to *root*."""
    return [c for c in citations if not (root / c.token).exists()]


def render(citations: Sequence[Citation], total: int) -> list[str]:
    """Render the report as the lines the CLI prints."""
    lines = [f"{c.file}:{c.line} {c.token} missing" for c in citations]
    lines.append(f"contributing-paths: {total} checked, {len(citations)} missing")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """Run the checker over the files named on the command line."""
    parser = argparse.ArgumentParser(description="Check the paths CONTRIBUTING.md cites.")
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args(argv)
    root = repo_root()
    entries = top_level_entries(root)
    citations: list[Citation] = []
    for path in args.files:
        if not path.is_file():
            print(f"{path}: no such file", file=sys.stderr)
            return EXIT_MISSING
        citations.extend(extract_citations(path, entries))
    missing = missing_citations(citations, root)
    for line in render(missing, len(citations)):
        print(line)
    return EXIT_MISSING if missing else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
