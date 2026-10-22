"""Judge a commit message header against Conventional Commits 1.0.0.

The header is the first line that is neither blank nor a git ``#``
comment. ``Merge``, ``Revert``, ``fixup!``, ``squash!`` and ``amend!``
headers are written by git itself and are exempt.
"""

from __future__ import annotations

import re

DEFAULT_TYPES: tuple[str, ...] = (
    "feat",
    "fix",
    "perf",
    "refactor",
    "docs",
    "test",
    "build",
    "ci",
    "chore",
    "style",
    "revert",
)
"""The closed type list, shared with Yoob's ``commit-msg`` hook."""

HEADER_MAX_CHARS = 72
"""Longest accepted header, counted in characters."""

EXEMPT_PREFIXES: tuple[str, ...] = ("Merge ", "Revert ", "fixup! ", "squash! ", "amend! ")
"""Headers git generates itself; never judged."""

_HEADER_RE = re.compile(rf"^({'|'.join(DEFAULT_TYPES)})(\([a-z0-9._/-]+\))?!?: \S.*$")


def message_header(message: str) -> str:
    """Return the first non-blank, non-comment line of ``message``."""
    for line in message.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            return line.rstrip()
    return ""


def header_violations(message: str) -> list[str]:
    """Name every rule the header of ``message`` breaks; empty when valid.

    Args:
        message: The full commit message, comments included.

    Returns:
        One human-readable rule per violation: empty message,
        not ``type(scope): subject``, longer than :data:`HEADER_MAX_CHARS`.
    """
    header = message_header(message)
    if not header:
        return ["the message is empty"]
    if header.startswith(EXEMPT_PREFIXES):
        return []
    violations: list[str] = []
    if not _HEADER_RE.match(header):
        violations.append(
            f"header does not match 'type(scope): subject' with type in {{{', '.join(DEFAULT_TYPES)}}}"
        )
    if len(header) > HEADER_MAX_CHARS:
        violations.append(f"header is {len(header)} characters; the limit is {HEADER_MAX_CHARS}")
    return violations
