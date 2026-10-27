"""Which spec a pull request is reviewed against (SP-UNIVERSAL-PROJECT-CONFIG G4).

Resolution order, restricted to ``[review].spec_sources``: the
``--spec`` flag, then the first ``Spec: SP-…`` line of the PR / MR
description, then the branch name (``<type>/sp-<id>…``, the historical
convention).  When none declares a spec, the PR is graded against every
governed spec owning a path of its diff (source ``ownership``,
SP-APPROVED-SPEC-WITHOUT-PLAN-GRADES-ITS-ACS G7); when no spec owns one
either, ``spec`` is ``null`` and spec coverage is ``not_applicable`` — so a
documentation or measurement branch is never refused for lacking a spec.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..review.spec import _normalise_id, detect_spec_from_branch, spec_exists
from .config import DEFAULT_SPEC_SOURCES, SpecSource

SPEC_LINE_RE = re.compile(r"^(?i:spec):\s*((?i:sp)-[A-Za-z0-9._-]+)\s*$", re.MULTILINE)
"""A description line declaring the spec, e.g. ``Spec: SP-YOOB-MR-CREATE``.

The ``Spec:`` key and the ``SP-`` prefix match in any case; the id is
returned verbatim, as written in the description."""


OwnershipSource = Literal["ownership"]
"""The source of the specs an undeclared PR is graded against (G7)."""


@dataclass(frozen=True)
class SpecResolution:
    """The resolved spec id and where it came from.

    Attributes:
        spec_id: Canonical ``SP-…`` id, or None when nothing declares one.
        source: ``flag``, ``description`` or ``branch``; None with no
            spec. A PR declaring none is then graded against the specs
            owning its code, and the review reports that source as
            ``ownership`` (:data:`OwnershipSource`,
            :attr:`repoach.review.spec_grading.CoverageGrading.source`).
    """

    spec_id: str | None = None
    source: SpecSource | OwnershipSource | None = None


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


def declared_spec_missing(declared: SpecResolution, root: Path) -> bool:
    """Return whether a description-declared spec names no document.

    Only the ``Spec:`` line counts: a ``--spec`` naming no document is
    refused before the review, and a branch-derived id with no document
    keeps its historical fallback (the decomposed-parent anchor).

    Args:
        declared: The resolved spec.
        root: Repository root holding the spec directory.

    Returns:
        True when the description named a spec that does not exist.
    """
    if declared.source != "description" or declared.spec_id is None:
        return False
    return not spec_exists(declared.spec_id, root=root)


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


def spec_coverage_label(spec_id: str | None, covered: bool | None, *, missing: bool = False) -> str:
    """Return the spec-coverage fact as reported in JSON payloads.

    Args:
        spec_id: The resolved spec, None when none is declared.
        covered: The coverage verdict, None when none was computed.
        missing: The declared spec has no document under the spec directory.

    Returns:
        ``not_applicable`` without a spec (never a refusal), ``missing``
        when the declared spec has no document, ``unknown`` when a spec
        is declared but no coverage was computed, else ``covered`` /
        ``not_covered``.
    """
    if spec_id is None:
        return "not_applicable"
    if missing:
        return "missing"
    if covered is None:
        return "unknown"
    return "covered" if covered else "not_covered"
