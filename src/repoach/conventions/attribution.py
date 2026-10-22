"""Recognise and remove AI attribution from commit messages and PR bodies.

Owner rule: no AI attribution anywhere — no ``Co-Authored-By`` trailer
naming a model or Anthropic, no ``Generated with … Claude Code`` footer,
no ``Claude-Session`` trailer. A human ``Co-Authored-By`` survives.

Matching is per line and anchored at the line start, so a body sentence
that merely mentions Claude (the ``claude_code`` chain, a model id) is
never touched: only a line shaped like a trailer or the footer is.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

ATTRIBUTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*co-authored-by:.*\b(claude|anthropic)\b", re.IGNORECASE),
    re.compile(r"^\s*(\U0001f916\s*)?generated (with|by)\b.*\b(claude|anthropic)\b", re.IGNORECASE),
    re.compile(r"^\s*claude-session:", re.IGNORECASE),
)
"""One pattern per attribution shape; a line matching any is attribution."""


def is_attribution_line(line: str) -> bool:
    """True when ``line`` is an AI attribution trailer or footer."""
    return any(pattern.search(line) for pattern in ATTRIBUTION_PATTERNS)


def attribution_lines(text: str) -> list[str]:
    """Return every attribution line of ``text``, stripped, in order."""
    return [line.strip() for line in text.splitlines() if is_attribution_line(line)]


def strip_attribution(text: str) -> str:
    """Remove attribution lines and the blank-line runs they leave behind.

    Consecutive blank lines collapse to one and trailing blank lines are
    dropped; a text with no attribution line is returned unchanged.

    Args:
        text: A commit message, squash body or PR description.

    Returns:
        ``text`` without any attribution line, ending with one newline
        when the input ended with one.
    """
    lines = text.splitlines()
    if not any(is_attribution_line(line) for line in lines):
        return text
    kept: list[str] = []
    for line in lines:
        if is_attribution_line(line):
            continue
        if not line.strip() and kept and not kept[-1].strip():
            continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    if not kept:
        return ""
    return "\n".join(kept) + ("\n" if text.endswith("\n") else "")


def squash_body(messages: Sequence[tuple[str, str]]) -> str:
    """Compose a squash commit body from ``(headline, body)`` pairs, attribution removed.

    One ``* headline`` block per commit, its body under it, blocks
    separated by a blank line — the shape GitHub gives a squash by
    default, minus every attribution line each commit carried.

    Args:
        messages: The PR's commits, oldest first.

    Returns:
        The body to pass to the forge's squash merge; ``""`` for no commit.
    """
    blocks: list[str] = []
    for headline, body in messages:
        cleaned = strip_attribution(body).strip()
        blocks.append(f"* {headline.strip()}" + (f"\n\n{cleaned}" if cleaned else ""))
    return "\n\n".join(blocks) + ("\n" if blocks else "")
