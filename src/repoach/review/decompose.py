"""Governed sub-spec decomposition front-end (SP-DEVAGENT-DECOMPOSE).

Slice 4 of the real-coding-agent arc (see ``docs/devagent_architecture.md``). A
governed parent spec is decomposed into **ordered governed sub-specs** whose
``owns`` form a **partition** of the parent's ``owns`` — each parent-owned entry is
assigned to exactly one sub-spec — with an acyclic ``depends_on`` ordering. Each
sub-spec then flows through the existing pipeline (Planner → agentic loop → self-
verify) in dependency order.

"Always decompose" is uniform: a small spec (≤1 owned code path — nothing to split
without finer granularity, which this slice does not introduce) yields a single
sub-spec equal to the parent, and the caller treats that as the unchanged
single-spec path. Larger specs go through an LLM proposer (OPUS, healthy — not the
coder tier) whose proposal is accepted only after **mechanical** validation:

* every sub-spec's ``owns`` entries are a subset of the parent's;
* the sub-specs' ``owns`` entries partition the parent's exactly (cover + disjoint,
  reusing :meth:`arch.registry.Registry.disjointness_violations`);
* each sub-spec's ``depends_on`` references only the parent's edges ∪ sibling
  sub-spec ids;
* the inter-sub-spec graph is acyclic (:meth:`arch.graph.Graph.cycles`), giving the
  dependency order.

A validation failure is fed back to the proposer for a bounded retry (mirroring the
Planner's parse/validate retry); persistent failure returns a loud error result —
the function never raises.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..agent_engine.agent_loop import AgentLoop
from ..arch.registry import Registry, SpecNode
from ..core.logging import get_logger
from ..llm.capability import CapabilityTier
from .governed_spec import GovernedSpec
from .spec import SpecPlan

_log = get_logger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts" / "review"
_PERSONA = "decomposer_0.1.1.md"
_MAX_ATTEMPTS = 3
_SUB_ID_RE = re.compile(r"^SP-[A-Z0-9-]+$")
"""A sub-spec id must be a clean SP-ID — it becomes a filename, so any path
separator or traversal sequence from the (LLM) proposer is refused outright."""

Decomposer = Callable[[str], str]
"""A decomposer takes the rendered prompt and returns the raw model reply."""


@dataclass(frozen=True)
class SubSpec:
    """One governed sub-spec of a decomposed parent.

    Attributes:
        id: The sub-spec SP-ID (unique; e.g. ``SP-PARENT-1``).
        title: Human title.
        summary: One-line summary.
        owns_code: Owned code paths (a subset of the parent's).
        owns_resources: Owned resource keys (a subset of the parent's).
        depends_on: Declared edges — parent edges and/or sibling sub-spec ids.
        body: The sub-spec's markdown body (Goals / Behavior / Acceptance …).
    """

    id: str
    title: str
    summary: str
    owns_code: tuple[str, ...]
    owns_resources: tuple[str, ...]
    depends_on: tuple[str, ...]
    body: str


@dataclass
class DecomposeResult:
    """Outcome of :func:`decompose_spec`.

    Attributes:
        sub_specs: The ordered sub-specs (dependency order). For the identity
            passthrough this is a single sub-spec equal to the parent.
        identity: ``True`` when the parent was small and returned unsplit (no
            proposer call, no new files).
        error: ``None`` on success; a short reason when decomposition failed (the
            caller must not develop). ``sub_specs`` is empty when ``error`` is set.
    """

    sub_specs: list[SubSpec] = field(default_factory=list)
    identity: bool = False
    error: str | None = None


def make_decomposer() -> Decomposer:
    """Build the production proposer: an OPUS-tier one-shot over the proxy.

    OPUS (healthy chain) brings heavier reasoning to the spec-splitting call than
    the coder tier, and is built lazily by the caller so a small-spec session that
    never splits spins up no loop.
    """
    loop = AgentLoop(capability=CapabilityTier.OPUS, max_tokens=4000, temperature=0.0)
    return lambda prompt: loop.run_oneshot(prompt, json_response=True).text


def _identity_sub_spec(spec: SpecPlan, governed: GovernedSpec) -> SubSpec:
    """Return the parent itself as a single sub-spec (the small-spec passthrough)."""
    return SubSpec(
        id=governed.id,
        title=spec.title,
        summary=spec.summary,
        owns_code=governed.owns_code,
        owns_resources=governed.owns_resources,
        depends_on=governed.depends_on,
        body=spec.raw_markdown,
    )


def _parse_proposal(raw: str) -> list[dict] | None:
    """Extract the ``sub_specs`` list from the proposer's JSON reply, or ``None``.

    Tolerant of prose/fences around the object; returns ``None`` when no object
    with a non-empty ``sub_specs`` list can be recovered.
    """
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError) as exc:
        _log.debug("decompose.proposal_json_decode_failed", error=str(exc)[:120])
        return None
    if not isinstance(data, dict):
        return None
    items = data.get("sub_specs")
    if not isinstance(items, list) or not items or not all(isinstance(i, dict) for i in items):
        return None
    return items


def _sub_specs_from_proposal(items: list[dict]) -> list[SubSpec] | None:
    """Build :class:`SubSpec` rows from proposal dicts, or ``None`` on a bad shape."""
    out: list[SubSpec] = []
    for item in items:
        sub_id = item.get("id")
        if not isinstance(sub_id, str) or not sub_id:
            return None
        out.append(
            SubSpec(
                id=sub_id,
                title=str(item.get("title", sub_id)),
                summary=str(item.get("summary", "")),
                owns_code=tuple(str(p) for p in item.get("owns_code", []) if isinstance(p, str)),
                owns_resources=tuple(
                    str(p) for p in item.get("owns_resources", []) if isinstance(p, str)
                ),
                depends_on=tuple(str(d) for d in item.get("depends_on", []) if isinstance(d, str)),
                body=str(item.get("body", "")),
            )
        )
    return out


def _validate_partition(sub_specs: list[SubSpec], governed: GovernedSpec) -> str:
    """Return ``""`` when the sub-specs partition the parent's owns, else a complaint.

    The owns entries (code + resources) of all sub-specs must equal the parent's set
    exactly with no entry assigned twice (cover + disjoint), no sub-spec may own an
    entry the parent does not, and every ``depends_on`` must reference the parent's
    edges or a sibling sub-spec id. Pairwise path nesting is caught by reusing the
    arch registry's disjointness check; cycles by the arch graph.
    """
    ids = [s.id for s in sub_specs]
    if len(ids) != len(set(ids)):
        return f"sub-spec ids are not unique: {ids}"
    bad_ids = [s.id for s in sub_specs if not _SUB_ID_RE.match(s.id)]
    if bad_ids:
        return f"sub-spec ids are not valid SP-IDs (expected ^SP-[A-Z0-9-]+$): {bad_ids}"

    parent_code = set(governed.owns_code)
    parent_resources = set(governed.owns_resources)
    sibling_ids = set(ids)
    allowed_edges = set(governed.depends_on) | sibling_ids

    flat_code: list[str] = []
    flat_resources: list[str] = []
    for sub in sub_specs:
        flat_code.extend(sub.owns_code)
        flat_resources.extend(sub.owns_resources)
        out_of_parent = [p for p in sub.owns_code if p not in parent_code]
        out_of_parent += [r for r in sub.owns_resources if r not in parent_resources]
        if out_of_parent:
            return f"sub-spec {sub.id} owns entries the parent does not: {out_of_parent}"
        bad_edges = [d for d in sub.depends_on if d not in allowed_edges]
        if bad_edges:
            return (
                f"sub-spec {sub.id} depends on {bad_edges} — only the parent's edges "
                f"{sorted(governed.depends_on)} or sibling sub-specs {sorted(sibling_ids)} are allowed"
            )

    if len(flat_code) != len(set(flat_code)):
        return f"a parent code path is claimed by more than one sub-spec: {sorted(flat_code)}"
    if len(flat_resources) != len(set(flat_resources)):
        return f"a parent resource is claimed by more than one sub-spec: {sorted(flat_resources)}"
    if set(flat_code) != parent_code:
        missing = sorted(parent_code - set(flat_code))
        return f"sub-specs do not cover the parent's code owns; uncovered: {missing}"
    if set(flat_resources) != parent_resources:
        missing = sorted(parent_resources - set(flat_resources))
        return f"sub-specs do not cover the parent's resource owns; uncovered: {missing}"

    registry = Registry(
        nodes={
            sub.id: SpecNode(
                id=sub.id,
                path=Path(sub.id),
                owns_code=sub.owns_code,
                owns_resources=sub.owns_resources,
                depends_on=sub.depends_on,
            )
            for sub in sub_specs
        }
    )
    conflicts = registry.disjointness_violations()
    if conflicts:
        detail = "; ".join(f"{c.artifact} owned by {list(c.spec_ids)}" for c in conflicts)
        return f"sub-spec owns overlap (nested paths): {detail}"
    cycles = registry.graph().cycles()
    if cycles:
        return f"sub-spec depends_on has a cycle: {cycles}"
    return ""


def _dependency_order(sub_specs: list[SubSpec]) -> list[SubSpec]:
    """Return the sub-specs in dependency order (a sub-spec after its siblings).

    Kahn's algorithm over the inter-sub-spec edges only (parent/external edges are
    ignored). Ties break on id for determinism; safe because the caller validated
    acyclicity first.
    """
    by_id = {s.id: s for s in sub_specs}
    sibling_ids = set(by_id)
    pending = {s.id: {d for d in s.depends_on if d in sibling_ids} for s in sub_specs}
    ordered: list[SubSpec] = []
    while pending:
        ready = sorted(sid for sid, deps in pending.items() if not deps)
        if not ready:
            ordered.extend(by_id[sid] for sid in sorted(pending))
            break
        for sid in ready:
            ordered.append(by_id[sid])
            del pending[sid]
        for deps in pending.values():
            deps.difference_update(ready)
    return ordered


def _render_decomposer_prompt(spec: SpecPlan, governed: GovernedSpec, feedback: str) -> str:
    """Substitute the parent spec, its owns/edges, and any retry feedback."""
    template = (_PROMPTS_DIR / _PERSONA).read_text(encoding="utf-8")
    resource_lines = [f"  - {r}" for r in governed.owns_resources] or ["  (none)"]
    owns_block = "\n".join(
        [
            "code:",
            *[f"  - {p}" for p in governed.owns_code],
            "resources:",
            *resource_lines,
            f"depends_on: {list(governed.depends_on)}",
        ]
    )
    return (
        template.replace("{SPEC_ID}", governed.id)
        .replace("{PARENT_OWNS}", owns_block)
        .replace("{SPEC_PLAN}", spec.raw_markdown)
        .replace("{FEEDBACK}", feedback or "(first attempt)")
    )


def decompose_spec(
    spec: SpecPlan,
    governed: GovernedSpec,
    *,
    proposer: Decomposer | None,
    repo_root: Path,
) -> DecomposeResult:
    """Decompose a governed parent spec into ordered governed sub-specs.

    Args:
        spec: The loaded parent spec (its markdown feeds the proposer).
        governed: The parent's governed frontmatter (owns/depends_on).
        proposer: The decomposition proposer, or ``None`` to build the production
            OPUS proposer lazily (only reached for a large spec).
        repo_root: Repository root (reserved for future owns resolution).

    Returns:
        A :class:`DecomposeResult`. The identity passthrough (≤1 owned code path)
        never calls the proposer. A proposal is returned only after it validates as
        a partition of the parent; persistent failure sets ``error``.
    """
    if len(governed.owns_code) <= 1:
        _log.info("decompose.identity", spec_id=governed.id, n_owns=len(governed.owns_code))
        return DecomposeResult(sub_specs=[_identity_sub_spec(spec, governed)], identity=True)

    judge = proposer if proposer is not None else make_decomposer()
    feedback = ""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        prompt = _render_decomposer_prompt(spec, governed, feedback)
        try:
            raw = judge(prompt)
        except Exception as exc:
            _log.warning("decompose.proposer_failed", spec_id=governed.id, error=str(exc)[:160])
            return DecomposeResult(error=f"decompose proposer failed: {str(exc)[:160]}")
        items = _parse_proposal(raw)
        sub_specs = _sub_specs_from_proposal(items) if items is not None else None
        if sub_specs is None:
            feedback = (
                "Your reply was not a JSON object with a non-empty 'sub_specs' list of objects."
            )
            _log.warning("decompose.proposal_unparseable", spec_id=governed.id, attempt=attempt)
            continue
        complaint = _validate_partition(sub_specs, governed)
        if not complaint:
            ordered = _dependency_order(sub_specs)
            _log.info(
                "decompose.accepted",
                spec_id=governed.id,
                n_sub_specs=len(ordered),
                attempt=attempt,
            )
            return DecomposeResult(sub_specs=ordered, identity=False)
        feedback = complaint
        _log.warning(
            "decompose.proposal_rejected", spec_id=governed.id, attempt=attempt, complaint=complaint
        )
    return DecomposeResult(error=f"decompose failed after {_MAX_ATTEMPTS} attempts: {feedback}")


def render_sub_spec_markdown(sub_spec: SubSpec) -> str:
    """Render a :class:`SubSpec` to a governed spec markdown document.

    The frontmatter carries the required governed keys (``id``/``title``/``version``/
    ``status``/``owns``/``depends_on``) so the arch registry loads it and
    ``arch graph --check`` validates it; the body follows verbatim.
    """
    resources = sub_spec.owns_resources or ()
    lines = [
        "---",
        f"id: {sub_spec.id}",
        f"title: {json.dumps(sub_spec.title)}",
        "version: 0.1",
        "status: draft",
        "author: agent",
        "",
        "owns:",
        "  code:",
        *[f"    - {p}" for p in sub_spec.owns_code],
        "  resources:" + (" []" if not resources else ""),
        *[f"    - {r}" for r in resources],
        "",
        f"depends_on: {list(sub_spec.depends_on)}",
        "provides_to: []",
        "constraints: {}",
        "---",
        "",
        sub_spec.body,
    ]
    return "\n".join(lines) + "\n"


__all__ = [
    "DecomposeResult",
    "Decomposer",
    "SubSpec",
    "decompose_spec",
    "make_decomposer",
    "render_sub_spec_markdown",
]
