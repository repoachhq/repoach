#!/usr/bin/env python3
"""Release notes from the Conventional Commit subjects between two refs (SP-PYPI-RELEASE G4).

Usage::

    python scripts/release_notes.py v0.0.1 v0.1.0

Prints Markdown on stdout: a ``## <tag>`` heading, then one section per
commit type in a fixed order. Breaking changes (``type!:`` or a
``BREAKING CHANGE:`` footer) are listed first, in their own section, and
again under their type. Merge commits are skipped; a subject that is not a
Conventional Commit header lands under ``Other``.

Exit codes:

* ``0`` notes printed.
* ``2`` a ref does not exist, or the directory is not a git repository.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SECTIONS: tuple[tuple[str, str], ...] = (
    ("feat", "Features"),
    ("fix", "Fixes"),
    ("perf", "Performance"),
    ("refactor", "Refactoring"),
    ("docs", "Documentation"),
)
OTHER = "Other"
BREAKING = "Breaking changes"

_HEADER = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?: (?P<subject>\S.*)$")
_BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE: ", re.MULTILINE)
_FIELD = "\x1f"
_RECORD = "\x1e"


class RefError(Exception):
    """A ref named on the command line does not resolve to a commit."""


@dataclass(frozen=True)
class Commit:
    """One commit of the range, parsed.

    Attributes:
        sha: Full commit SHA.
        type: Conventional Commit type, or ``""`` when the subject is not a header.
        scope: Scope inside the parentheses, ``""`` when absent.
        subject: Text after ``type(scope): ``, or the whole subject line.
        breaking: ``type!:`` or a ``BREAKING CHANGE:`` footer.
    """

    sha: str
    type: str
    scope: str
    subject: str
    breaking: bool


def parse_commit(sha: str, subject: str, body: str) -> Commit:
    """Parse one commit's subject line and body.

    Args:
        sha: Commit SHA.
        subject: First line of the message.
        body: Rest of the message.

    Returns:
        The parsed commit.
    """
    match = _HEADER.match(subject.strip())
    breaking = bool(_BREAKING_FOOTER.search(body))
    if match is None:
        return Commit(sha=sha, type="", scope="", subject=subject.strip(), breaking=breaking)
    return Commit(
        sha=sha,
        type=match["type"],
        scope=match["scope"] or "",
        subject=match["subject"].strip(),
        breaking=breaking or match["bang"] is not None,
    )


def _line(commit: Commit) -> str:
    scope = f"**{commit.scope}:** " if commit.scope else ""
    return f"- {scope}{commit.subject} ({commit.sha[:7]})"


def render(tag: str, commits: list[Commit]) -> str:
    """Render the notes for ``tag``.

    Args:
        tag: The release tag, used as the heading.
        commits: Commits of the range, newest first.

    Returns:
        Markdown text ending with a newline.
    """
    out = [f"## {tag}"]
    if not commits:
        out += ["", "No changes."]
        return "\n".join(out) + "\n"
    groups: list[tuple[str, list[Commit]]] = [
        (BREAKING, [c for c in commits if c.breaking]),
    ]
    known = {kind for kind, _ in SECTIONS}
    for kind, title in SECTIONS:
        groups.append((title, [c for c in commits if c.type == kind]))
    groups.append((OTHER, [c for c in commits if c.type not in known]))
    for title, members in groups:
        if not members:
            continue
        out += ["", f"### {title}", ""]
        out += [_line(c) for c in members]
    return "\n".join(out) + "\n"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def _resolve(repo: Path, ref: str) -> str:
    completed = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RefError(ref)
    return completed.stdout.strip()


def collect(repo: Path, prev: str, tag: str) -> list[Commit]:
    """Return the non-merge commits reachable from ``tag`` and not from ``prev``.

    Args:
        repo: Repository directory.
        prev: The previous release ref.
        tag: The release ref.

    Returns:
        Parsed commits, newest first.

    Raises:
        RefError: ``prev`` or ``tag`` does not resolve to a commit.
    """
    start = _resolve(repo, prev)
    end = _resolve(repo, tag)
    completed = _git(
        repo,
        "log",
        "--no-merges",
        f"--format=%H{_FIELD}%s{_FIELD}%b{_RECORD}",
        f"{start}..{end}",
    )
    if completed.returncode != 0:
        raise RefError(f"{prev}..{tag}")
    commits = []
    for record in completed.stdout.split(_RECORD):
        record = record.strip("\n")
        if not record:
            continue
        sha, subject, body = record.split(_FIELD, 2)
        commits.append(parse_commit(sha, subject, body))
    return commits


def main(argv: list[str] | None = None) -> int:
    """Print the release notes for ``PREV..TAG``; see the module docstring."""
    parser = argparse.ArgumentParser(prog="release_notes", description=__doc__.splitlines()[0])
    parser.add_argument("prev", help="Previous release tag (or any commit-ish).")
    parser.add_argument("tag", help="The release tag.")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Repository directory.")
    args = parser.parse_args(argv)
    try:
        commits = collect(args.repo, args.prev, args.tag)
    except RefError as exc:
        print(f"release_notes: no such ref: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(render(args.tag, commits))
    return 0


if __name__ == "__main__":
    sys.exit(main())
