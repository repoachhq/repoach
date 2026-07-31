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


def _find_repo_root(start: Path) -> Path:
    """Walk upward from *start* to the nearest ancestor containing ``pyproject.toml``.

    Falls back to *start* itself when no ancestor has one (e.g. the
    module was copied out of the repo), so root resolution degrades
    to CWD-relative behavior rather than raising.
    """
    for candidate in (start, *start.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return start


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
    repo_root = _find_repo_root(Path(__file__).resolve().parent)
    candidate_roots = [repo_root / r for r in raw_roots]
    roots = [r for r in candidate_roots if r.is_dir()]

    if not roots:
        missing = ", ".join(str(r) for r in candidate_roots)
        print(
            f"error: no configured scan roots exist (resolved against repo root "
            f"{repo_root}): {missing}"
        )
        return 2

    violations = scan(roots)
    counts = summarise(violations)

    summary_line = (
        "pass={pass} return={return} continue={continue} ellipsis={ellipsis} "
        "assign={assign} suppress={suppress} unparseable={unparseable} "
        "total={total} files={files}"
    ).format(**counts)

    if args.summary:
        print(summary_line)
    else:
        for v in violations:
            print(v.format())
        if violations:
            print("---")
        print(summary_line)

    return 1 if counts["total"] > args.max else 0


if __name__ == "__main__":
    sys.exit(main())
