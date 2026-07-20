#!/usr/bin/env python3
"""CLI wrapper around ``repoach.lint.no_silent_except``.

SP-LINT-LOG-CATCH-ALL.

Usage examples::

    python scripts/lint_no_silent_except.py
    python scripts/lint_no_silent_except.py --summary
    python scripts/lint_no_silent_except.py --root src/repoach/lint
    python scripts/lint_no_silent_except.py --max 100

Mirrors :mod:`scripts.lint_no_inline_comments` so the operator
experience is uniform across the two lints. The pytest binding
(``tests/unit/test_no_silent_except_gate.py``) imports the scanner
directly; this wrapper exists so the pre-commit hook and ad-hoc
local runs can invoke a single executable.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from repoach.lint.no_silent_except import (
    DEFAULT_ROOTS,
    MAX_SILENT_EXCEPT,
    scan,
    summarise,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lint_no_silent_except",
        description=(
            "Report ``except`` handlers that swallow without logging. "
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
        default=MAX_SILENT_EXCEPT,
        metavar="N",
        help=(
            "Maximum violations to tolerate before exiting non-zero. "
            f"Defaults to MAX_SILENT_EXCEPT={MAX_SILENT_EXCEPT} "
            "(the ratchet baseline; lower it as logging cleanups land)."
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
        print(
            "pass={pass} return={return} continue={continue} "
            "ellipsis={ellipsis} assign={assign} total={total} files={files}".format(**counts)
        )
    else:
        for v in violations:
            print(v.format())
        if violations:
            print("---")
        print(
            "pass={pass} return={return} continue={continue} "
            "ellipsis={ellipsis} assign={assign} total={total} files={files}".format(**counts)
        )

    return 1 if counts["total"] > args.max else 0


if __name__ == "__main__":
    sys.exit(main())
