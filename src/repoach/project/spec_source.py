"""Which spec a pull request is reviewed against (SP-UNIVERSAL-PROJECT-CONFIG G4).

Resolution order, restricted to ``[review].spec_sources``: the
``--spec`` flag, then the first ``Spec: SP-…`` line of the PR / MR
description, then the branch name (``<type>/sp-<id>…``, the historical
convention).  When none declares a spec the review runs without one —
``spec`` is ``null`` and spec coverage is ``not_applicable`` — so a
documentation or measurement branch is never refused for lacking a spec.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..review.spec import _normalise_id, detect_spec_from_branch
from .config import DEFAULT_SPEC_SOURCES, SpecSource

SPEC_LINE_RE = re.compile(r"^Spec:\s*(SP-[A-Z0-9-]+)\s*$", re.MULTILINE)
"""A description line declaring the spec, e.g. ``Spec: SP-YOOB-MR-CREATE``."""


@dataclass(frozen=True)
class SpecResolution:
    """The resolved spec id and where it came from.

    Attributes:
        spec_id: Canonical ``SP-…`` id, or None when nothing declares one.
        source: ``flag``, ``description`` or ``branch``; None with no spec.
    """

    spec_id: str | None = None
    source: SpecSource | None = None


def spec_from_description(description: str) -> str | None:
    """Return the id of the first ``Spec:`` line of a description, if any.

    Args:
        description: PR / MR body.

    Returns:
        The id, or None.
    """
    match = SPEC_LINE_RE.search(description or "")
    return match.group(1) if match is not None else None


def resolve_spec(
    *,
    flag: str | None,
    description: str,
    branch: str,
    sources: Sequence[SpecSource] = DEFAULT_SPEC_SOURCES,
    root: Path | None = None,
) -> SpecResolution:
    """Resolve the spec of a pull request.

    Args:
        flag: The ``--spec`` value, if given.
        description: The PR / MR description.
        branch: The head branch name.
        sources: Which sources to consult, in order.
        root: Repository root, used by the branch parser to prefer the
            longest spec id present under the spec directory.

    Returns:
        The id and its source; an empty resolution when none applies.
    """
    for source in sources:
        if source == "flag" and flag and flag.strip():
            return SpecResolution(_normalise_id(flag), "flag")
        if source == "description":
            found = spec_from_description(description)
            if found is not None:
                return SpecResolution(found, "description")
        if source == "branch" and branch:
            detected = detect_spec_from_branch(branch, root=root)
            if detected:
                return SpecResolution(detected, "branch")
    return SpecResolution()


def resolve_spec_id(
    *,
    flag: str | None,
    description: str,
    branch: str,
    sources: tuple[SpecSource, ...] = DEFAULT_SPEC_SOURCES,
    root: Path | None = None,
) -> str | None:
    """Return only the id :func:`resolve_spec` resolves (the spec's interface).

    Args:
        flag: The ``--spec`` value, if given.
        description: The PR / MR description.
        branch: The head branch name.
        sources: Which sources to consult, in order.
        root: Repository root for the branch parser.

    Returns:
        The canonical spec id, or None.
    """
    return resolve_spec(
        flag=flag, description=description, branch=branch, sources=sources, root=root
    ).spec_id


def spec_coverage_label(spec_id: str | None, covered: bool | None) -> str:
    """Return the spec-coverage fact as reported in JSON payloads.

    Args:
        spec_id: The resolved spec, None when none is declared.
        covered: The coverage verdict, None when none was computed.

    Returns:
        ``not_applicable`` without a spec (never a refusal), ``unknown``
        when a spec is declared but no coverage was computed, else
        ``covered`` / ``not_covered``.
    """
    if spec_id is None:
        return "not_applicable"
    if covered is None:
        return "unknown"
    return "covered" if covered else "not_covered"
