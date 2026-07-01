"""Anchor a decomposed PR's review to its governed sub-specs (SP-DEVAGENT-REVIEW-ANCHOR).

When a governed parent spec is decomposed, SP-DEVAGENT-WIRE deletes the parent spec
file (so the arch disjointness gate stays green — the sub-specs are the sole owners).
The review orchestrator resolves a PR's spec from its branch via
:func:`ferova.review.spec.maybe_load_active_spec`, which then returns ``None`` (the
parent file is gone), dropping the reviewers to a spec-unaware review.

This module restores a spec-aware review for such a PR: it recovers the parent id from
the branch, confirms the parent governed spec no longer loads (it was superseded),
enumerates the decomposition sub-specs (named ``<PARENT-ID>-<N>`` by the decompose
front-end), and renders their concatenated markdown + per-sub-spec architecture edges
into the single ``spec_plan`` / ``arch_edges`` strings the reviewer contract already
accepts. The single-spec and frontier paths are untouched, and every entry point is
best-effort: any failure logs and yields ``None`` / ``[]`` rather than raising.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..arch import load_registry
from ..core.logging import get_logger
from .governed_spec import load_governed_spec, render_arch_edges
from .spec import SPECS_DIR, SpecPlan, load_spec

_log = get_logger(__name__)

_PARENT_BRANCH_RE = re.compile(r"^[a-z]+/sp-([a-z0-9-]+?)(?:-impl|-fix)?$", re.IGNORECASE)
"""Recover the FULL parent slug from a ``feat/sp-<slug>-impl`` branch.

Unlike :func:`ferova.review.spec.detect_spec_from_branch`, this is anchored to the
end of the string and does not truncate a multi-segment id (``sp-a-b-c-impl`` →
``a-b-c``, not ``a``). The decompose branch is the parent's, and the parent spec file
is gone (superseded), so the on-disk greedy match cannot recover the id here."""


def _parent_id_from_branch(branch: str) -> str | None:
    """Return the canonical parent ``SP-<ID>`` encoded in *branch*, or ``None``."""
    match = _PARENT_BRANCH_RE.match(branch)
    if match is None:
        return None
    return f"SP-{match.group(1).upper()}"


@dataclass(frozen=True)
class AnchoredReview:
    """The review inputs for a decomposed PR, anchored to its sub-specs.

    Attributes:
        parent_id: The superseded parent SP-ID recovered from the branch.
        sub_spec_ids: The discovered sub-spec ids, in decomposition order.
        spec_plan_md: The concatenated sub-spec markdown fed to every reviewer.
        arch_edges: The joined per-sub-spec architecture-edges block.
    """

    parent_id: str
    sub_spec_ids: tuple[str, ...]
    spec_plan_md: str
    arch_edges: str


def discover_sub_specs(parent_id: str, *, root: Path | None = None) -> list[SpecPlan]:
    """Return the decomposition sub-specs of *parent_id*, in numeric order.

    Enumerates the arch registry for governed ids matching ``^<parent_id>-<digits>$``
    (the decompose naming convention) and loads each as a :class:`SpecPlan`. Unrelated
    ids (a different prefix, or a non-numeric suffix like ``SP-PARENT-FOO``) are
    ignored. Returns ``[]`` when none match or the corpus cannot be read.

    Args:
        parent_id: The parent SP-ID (any spelling; upper-cased here).
        root: Repository root override (tests).

    Returns:
        The sub-specs sorted by their numeric suffix; empty on no match / load error.
    """
    canonical = parent_id.strip().upper()
    pattern = re.compile(rf"^{re.escape(canonical)}-(\d+)$")
    specs_dir = (root or Path.cwd()) / SPECS_DIR
    try:
        registry = load_registry(specs_dir)
    except Exception as exc:
        _log.warning(
            "subspec_anchor.registry_load_failed", parent_id=canonical, error=str(exc)[:160]
        )
        return []
    matched: list[tuple[int, str]] = []
    for node_id, node in registry.nodes.items():
        if node.frontier:
            continue
        m = pattern.match(node_id)
        if m is not None:
            matched.append((int(m.group(1)), node_id))
    out: list[SpecPlan] = []
    for _num, sub_id in sorted(matched):
        try:
            out.append(load_spec(sub_id, root=root))
        except (FileNotFoundError, ValueError) as exc:
            _log.warning("subspec_anchor.sub_spec_load_failed", sub_id=sub_id, error=str(exc)[:160])
    return out


def render_anchored_review_inputs(
    sub_specs: Sequence[SpecPlan], *, root: Path | None = None
) -> tuple[str, str]:
    """Render ``(spec_plan_md, arch_edges)`` for a decomposed PR's sub-specs.

    The markdown opens with a header stating the PR implements a decomposed parent,
    then each sub-spec's markdown under a ``## Sub-spec <id>: <title>`` heading. The
    edges are each sub-spec's non-empty :func:`render_arch_edges` block, joined.

    Args:
        sub_specs: The discovered sub-specs (decomposition order).
        root: Repository root override (tests).

    Returns:
        ``(concatenated_markdown, joined_arch_edges)``.
    """
    header = (
        "This PR implements a DECOMPOSED parent spec — superseded and removed on "
        f"decomposition into {len(sub_specs)} governed sub-specs. Review the diff "
        "against ALL sub-specs below; each is a first-class governed spec and its "
        "file is part of the diff."
    )
    blocks = [header]
    edge_blocks: list[str] = []
    for sub in sub_specs:
        blocks.append(f"## Sub-spec {sub.id}: {sub.title}\n\n{sub.raw_markdown}")
        edges = render_arch_edges(load_governed_spec(sub.id, root=root))
        if edges:
            edge_blocks.append(edges)
    return "\n\n---\n\n".join(blocks), "\n\n".join(edge_blocks)


def maybe_anchor_decomposed_parent(
    branch: str, *, root: Path | None = None
) -> AnchoredReview | None:
    """Return anchored review inputs when *branch*'s parent spec was superseded.

    Returns an :class:`AnchoredReview` only when (a) a parent id parses from the
    branch, (b) that parent's governed spec no longer loads (it was deleted on
    decomposition), and (c) at least one sub-spec is discovered. Otherwise returns
    ``None`` — the caller keeps its spec-unaware fallback. Never raises.

    Args:
        branch: The PR head branch (``feat/sp-<parent>-impl``).
        root: Repository root override (tests).

    Returns:
        The anchored inputs, or ``None`` when the PR is not a superseded-parent case.
    """
    if not branch:
        return None
    parent_id = _parent_id_from_branch(branch)
    if parent_id is None:
        return None
    try:
        if load_governed_spec(parent_id, root=root) is not None:
            return None
    except Exception as exc:
        _log.warning(
            "subspec_anchor.parent_probe_failed", parent_id=parent_id, error=str(exc)[:160]
        )
        return None
    sub_specs = discover_sub_specs(parent_id, root=root)
    if not sub_specs:
        return None
    spec_plan_md, arch_edges = render_anchored_review_inputs(sub_specs, root=root)
    _log.info("subspec_anchor.anchored", parent_id=parent_id, n_sub_specs=len(sub_specs))
    return AnchoredReview(
        parent_id=parent_id,
        sub_spec_ids=tuple(s.id for s in sub_specs),
        spec_plan_md=spec_plan_md,
        arch_edges=arch_edges,
    )


__all__ = [
    "AnchoredReview",
    "discover_sub_specs",
    "maybe_anchor_decomposed_parent",
    "render_anchored_review_inputs",
]
