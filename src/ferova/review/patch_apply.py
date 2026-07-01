"""Anchored search/replace edit application (SP-DEV-TARGETED-PATCH).

Full-file re-emission of 1 200+ line modules is beyond reliable model
output — two redesign slices in a row stalled on it (unparseable first
attempts, shrinkage-guard rejections on retry). Anchored edits flip
the contract: the Developer copies an exact existing snippet and
provides its replacement; this module applies them deterministically
and turns every failure into directive feedback (what was not found,
what matched too often), so a retry converges instead of regenerating
the whole file from memory. Literal matching only — no regex, no
line numbers, the things chain models are reliably bad at.
"""

from __future__ import annotations

import difflib


def _closest_line(content: str, needle: str) -> str:
    """Return the file line most similar to the needle's first line.

    Args:
        content: Current full file contents.
        needle: Search text whose first line will be matched against the content.

    Returns:
        The closest matching line from the content, or a fallback message if no
        similar line is found.
    """
    first = needle.splitlines()[0] if needle.splitlines() else needle
    matches = difflib.get_close_matches(first, content.splitlines(), n=1, cutoff=0.3)
    return matches[0] if matches else "(no similar line found)"


def apply_search_replace_edits(content: str, edits: list[dict[str, str]]) -> tuple[str | None, str]:
    """Apply anchored edits to *content*, in order.

    Each edit is ``{"search": <exact unique snippet>, "replace":
    <replacement>}``. The search text must occur exactly once in the
    current content (uniqueness keeps the edit anchored without line
    numbers).

    Args:
        content: Current full file contents.
        edits: Ordered edit dicts; non-dict items, non-string fields
            and empty ``search`` values are violations.

    Returns:
        ``(new_content, "")`` when every edit applied, else
        ``(None, report)`` where the report says, per failing edit,
        whether the anchor was absent (with the closest existing
        line) or ambiguous (with the match count) — feedback a retry
        can act on directly.
    """
    current = content
    for index, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict):
            return None, f"edit {index}: not an object"
        search = edit.get("search")
        replace = edit.get("replace")
        if not isinstance(search, str) or not search:
            return None, f"edit {index}: 'search' must be a non-empty string"
        if not isinstance(replace, str):
            return None, f"edit {index}: 'replace' must be a string"
        occurrences = current.count(search)
        if occurrences == 0:
            return None, (
                f"edit {index}: search text not found in the file — copy it "
                f"VERBATIM from the current contents; closest existing line: "
                f"{_closest_line(current, search)!r}"
            )
        if occurrences > 1:
            return None, (
                f"edit {index}: search text matches {occurrences} times — "
                "extend it with surrounding lines until it is unique"
            )
        current = current.replace(search, replace, 1)
    return current, ""
