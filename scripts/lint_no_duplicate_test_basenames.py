#!/usr/bin/env python3
"""CLI wrapper around ``repoach.lint.no_duplicate_test_basenames``.

SP-TEST-BASENAME-GATE.

Usage examples::

    python scripts/lint_no_duplicate_test_basenames.py
    python scripts/lint_no_duplicate_test_basenames.py --summary
    python scripts/lint_no_duplicate_test_basenames.py --unit-root tests/unit
    python scripts/lint_no_duplicate_test_basenames.py --max 6

Mirrors :mod:`scripts.lint_no_silent_except` so the operator
experience is uniform across the three lints. The pytest binding
(``tests/unit/test_no_duplicate_test_basenames_gate.py``) imports the
scanner directly; this wrapper exists so the pre-commit hook and
ad-hoc local runs can invoke a single executable.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from repoach.lint.no_duplicate_test_basenames import (
    DEFAULT_INTEGRATION_ROOT,
    DEFAULT_UNIT_ROOT,
    MAX_DUPLICATES,
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
        prog="lint_no_duplicate_test_basenames",
        description=(
            "Report test-file basenames present in both tests/unit and "
            "tests/integration. Exits non-zero when the duplicate count "
            "exceeds ``--max``."
        ),
    )
    p.add_argument(
        "--unit-root",
        default=DEFAULT_UNIT_ROOT,
        metavar="DIR",
        help=f"Unit-suite directory to scan. Defaults to: {DEFAULT_UNIT_ROOT}.",
    )
    p.add_argument(
        "--integration-root",
        default=DEFAULT_INTEGRATION_ROOT,
        metavar="DIR",
        help=f"Integration-suite directory to scan. Defaults to: {DEFAULT_INTEGRATION_ROOT}.",
    )
    p.add_argument(
        "--max",
        type=int,
        default=MAX_DUPLICATES,
        metavar="N",
        help=(
            "Maximum duplicate basenames to tolerate before exiting non-zero. "
            f"Defaults to MAX_DUPLICATES={MAX_DUPLICATES} "
            "(the ratchet baseline; lower it as colliding pairs are renamed)."
        ),
    )
    p.add_argument(
        "--summary",
        action="store_true",
        help="Print only the total count; suppress the per-basename listing.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the desired process exit code."""
    args = _build_parser().parse_args(argv)
    repo_root = _find_repo_root(Path(__file__).resolve().parent)
    unit_root = repo_root / args.unit_root
    integration_root = repo_root / args.integration_root

    duplicates = scan(unit_root, integration_root)
    counts = summarise(duplicates)

    summary_line = f"total={counts['total']}"

    if args.summary:
        print(summary_line)
    else:
        for name in duplicates:
            print(name)
        if duplicates:
            print("---")
        print(summary_line)

    return 1 if counts["total"] > args.max else 0


if __name__ == "__main__":
    sys.exit(main())
