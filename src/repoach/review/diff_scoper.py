"""Diff scoper: splits raw diffs into FileDiff units and packs them under a char cap."""

from __future__ import annotations

from pydantic import BaseModel


class FileDiff(BaseModel):
    """A single file's diff block."""

    path: str
    text: str
    chars: int


class ScopedDiff(BaseModel):
    """Result of scoping a diff to a character cap.

    Attributes:
        prompt_diff: The diff text to include in the prompt, containing only whole file units.
        included: List of paths included in prompt_diff.
        omitted: List of paths omitted due to character cap.
        oversized: List of paths whose individual size exceeds the cap.
    """

    prompt_diff: str
    included: list[str]
    omitted: list[str]
    oversized: list[str]


def split_diff(diff: str) -> list[FileDiff]:
    """Split a raw unified diff into per-file FileDiff units.

    Args:
        diff: Raw unified diff string, possibly with preamble text before
            the first ``diff --git`` marker.

    Returns:
        List of FileDiff objects, one per file. Any text before the first
        ``diff --git`` marker is attached to the first unit's text.
        Returns an empty list for empty input.
    """
    if not diff:
        return []

    lines = diff.splitlines(keepends=True)
    split_indices: list[int] = [i for i, line in enumerate(lines) if line.startswith("diff --git ")]

    if not split_indices:
        return []

    preamble = "".join(lines[: split_indices[0]])
    units: list[FileDiff] = []

    for idx, start in enumerate(split_indices):
        end = split_indices[idx + 1] if idx + 1 < len(split_indices) else len(lines)
        unit_lines = lines[start:end]
        text = "".join(unit_lines)

        if idx == 0 and preamble:
            text = preamble + text

        # Parse path from "diff --git a/<path> b/<path>"
        header = unit_lines[0].strip()
        parts = header.split(" ")
        b_side = parts[3] if len(parts) >= 4 else ""
        path = b_side[2:] if b_side.startswith("b/") else b_side

        units.append(FileDiff(path=path, text=text, chars=len(text)))

    return units


def scope_diff(diff: str, cap_chars: int) -> ScopedDiff:
    """Greedily pack complete FileDiff units under *cap_chars*.

    Args:
        diff: Raw unified diff string.
        cap_chars: Maximum number of characters allowed in the returned
            ``prompt_diff``. Units that individually exceed this cap are
            recorded as oversized and never cut mid-text.

    Returns:
        ScopedDiff with ``prompt_diff`` containing only whole file units,
        ``included`` listing included paths, ``omitted`` listing left-out
        paths, and ``oversized`` listing paths whose size exceeds *cap_chars*.
    """
    units = split_diff(diff)

    if not units:
        return ScopedDiff(prompt_diff="", included=[], omitted=[], oversized=[])

    included: list[str] = []
    omitted: list[str] = []
    oversized: list[str] = []
    selected_texts: list[str] = []
    running_total = 0

    for unit in units:
        if unit.chars > cap_chars:
            oversized.append(unit.path)
            omitted.append(unit.path)
        elif running_total + unit.chars <= cap_chars:
            included.append(unit.path)
            selected_texts.append(unit.text)
            running_total += unit.chars
        else:
            omitted.append(unit.path)

    prompt_diff = "".join(selected_texts)

    if omitted:
        announcement = f"[diff scoped: {len(included)} of {len(units)} files shown COMPLETE. NOT visible (do not guess about them): {', '.join(omitted)}]"
        prompt_diff += "\n\n" + announcement

    return ScopedDiff(
        prompt_diff=prompt_diff,
        included=included,
        omitted=omitted,
        oversized=oversized,
    )
