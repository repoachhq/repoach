#!/usr/bin/env python3
"""Fail when a PR's commits, title or description carry AI attribution (SP-CONVENTIONS-GATE G9).

The commit-msg hook strips attribution before a commit lands and the
squash merge strips it from the merge body; this is the CI layer that
stops whatever slipped past both — a hook that was not installed, a
commit made with ``--no-verify``, a PR description pasted from an agent.

Usage::

    python scripts/lint_no_ai_attribution.py --range BASE..HEAD
    python scripts/lint_no_ai_attribution.py --text-env PR_BODY --text-env PR_TITLE

``--range`` judges every commit ``git rev-list`` lists for the range,
merge commits included. ``--text-env NAME`` judges the text held by the
environment variable ``NAME`` — CI passes the PR body that way so that
no shell ever interpolates it. Both options repeat and combine.

Exit codes: ``0`` nothing found (last line ``ai-attribution: ok``);
``1`` at least one attribution line, one ``<where>: <line>`` each;
``2`` the range could not be read.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence

from repoach.conventions.attribution import attribution_lines

EXIT_OK = 0
EXIT_FOUND = 1
EXIT_ERROR = 2


class RangeError(RuntimeError):
    """``git`` could not list or read the commits of a range."""


def _git(*args: str) -> str:
    """Run ``git`` and return stdout; raise :class:`RangeError` on failure."""
    res = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RangeError(f"git {' '.join(args)}: {res.stderr.strip()[:200]}")
    return res.stdout


def range_findings(revision_range: str) -> list[str]:
    """One ``<short-sha>: <line>`` per attribution line in the range's commits.

    Raises:
        RangeError: The range does not resolve or a commit cannot be read.
    """
    findings: list[str] = []
    for sha in _git("rev-list", "--reverse", revision_range).split():
        message = _git("log", "-1", "--format=%B", sha)
        findings.extend(f"commit {sha[:12]}: {line}" for line in attribution_lines(message))
    return findings


def text_findings(name: str, text: str) -> list[str]:
    """One ``<name>: <line>`` per attribution line in ``text``."""
    return [f"{name}: {line}" for line in attribution_lines(text)]


def main(argv: Sequence[str] | None = None) -> int:
    """Judge the given ranges and texts; print each finding."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--range", dest="ranges", action="append", default=[])
    parser.add_argument("--text-env", dest="text_envs", action="append", default=[])
    args = parser.parse_args(argv)
    findings: list[str] = []
    for revision_range in args.ranges:
        try:
            findings.extend(range_findings(revision_range))
        except RangeError as exc:
            print(f"ai-attribution: {exc}", file=sys.stderr)
            return EXIT_ERROR
    for name in args.text_envs:
        findings.extend(text_findings(name, os.environ.get(name, "")))
    for finding in findings:
        print(finding)
    if findings:
        print(f"ai-attribution: {len(findings)} attribution line(s) found; remove them")
        return EXIT_FOUND
    print("ai-attribution: ok")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
