#!/usr/bin/env python3
"""CLI wrapper around ``repoach.lint.no_inline_comments``.

SP-NO-INLINE-COMMENTS-GATE.

Usage examples::

    python scripts/lint_no_inline_comments.py
    python scripts/lint_no_inline_comments.py --summary
    python scripts/lint_no_inline_comments.py --root src/ferova/lint
    python scripts/lint_no_inline_comments.py --max 200

The pytest binding (``tests/unit/test_no_inline_comments_gate.py``)
imports the underlying scanner directly. This wrapper exists so
operators can run the gate locally without a pytest invocation, and
so a future pre-commit hook can call it as a standalone executable.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from repoach.lint.no_inline_comments import (
    DEFAULT_ROOTS,
    scan,
    summarise,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lint_no_inline_comments",
        description=(
            "Report inline ``#``-comments and ``# noqa`` directives. "
            "Exits non-zero when the violation count exceeds ``--max``."
        ),
    )
    p.add_argument(
        "--root",
        action="append",
        metavar="DIR",
        help=("Repeatable. Defaults to: " + ", ".join(DEFAULT_ROOTS) + "."),
    )
    p.add_argument(
        "--max",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Maximum violations to tolerate before exiting non-zero. "
            "Defaults to 0 (any violation fails)."
        ),
    )
    p.add_argument(
        "--summary",
        action="store_true",
        help="Print only the bucketed counts; suppress per-line listings.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the desired process exit code."""
    args = _build_parser().parse_args(argv)
    raw_roots = args.root if args.root else DEFAULT_ROOTS
    roots = [Path(r) for r in raw_roots]

    violations = scan(roots)
    counts = summarise(violations)

    if args.summary:
        print("inline={inline} noqa={noqa} total={total} files={files}".format(**counts))
    else:
        for v in violations:
            print(v.format())
        if violations:
            print("---")
        print("inline={inline} noqa={noqa} total={total} files={files}".format(**counts))

    return 1 if counts["total"] > args.max else 0


if __name__ == "__main__":
    sys.exit(main())
