"""SP-CHAINPILOT-PLAN — orchestrate a shadow chains.env rewrite (Phase 3d-1c).

The integration layer of 3d: given the current ``chains.env`` text, the decision
engine's planned mutations (3b), the benchmark prior (1c/3d-0), the equivalences
(1d), the live matrix (1b) and the set of healthy cells (2a), it produces the
**shadow** rewrite — the new file content plus the journalled mutations — without
touching disk. 3d-2 (flag-gated) does the actual write; 3e wires the cadence that
gathers the live inputs and calls this.

Two things only this layer can do (the reason cold-start moved here from 3b):
- **Cold-start placement.** A benchmark model absent from every chain, whose tier
  the placement classifier (3d-1b) assigns and for which the live matrix offers a
  provider, is trialled by inserting it into that tier — at most one per tier per
  cycle (the conservative default, a design parameter), so the loop trials
  gradually rather than flooding a chain.
- **Provider resolution.** The placement works in benchmark-canonical space; this
  layer resolves a canonical to a concrete ``(provider, model_id)`` cell via the
  equivalences + the live matrix, preferring a cell observed healthy.

Profiles are built **joined** (per-source name fragments collapsed under their
canonical) so a model's metrics are not split across differently-named fragments.
Closed models whose fragments do not join still cannot be cold-started — they have no
cell in our matrix, so provider resolution drops them — which keeps the cold-start
set to genuinely reachable models.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from repoach.llm_proxy.providers.benchmark_equivalences import EquivalenceTable
from repoach.llm_proxy.providers.benchmark_prior import BenchmarkRanking
from repoach.llm_proxy.providers.model_matrix import ModelCell, ProviderModelMatrix
from repoach.llm_proxy.routing.chain import Chain
from repoach.review.chain_placement import Placement, place_candidates, profiles_from_ranking
from repoach.review.chain_rewrite import ChainEdit, EditOp, RewriteResult, rewrite_chains
from repoach.review.decision import MutationKind, PlannedMutation

_SLOT_KEYS = {
    "MODEL_OPUS": "opus",
    "MODEL_SONNET": "sonnet",
    "MODEL_HAIKU": "haiku",
}

_MUTATION_TO_EDIT_OP = {
    MutationKind.EVICT_MODEL: EditOp.EVICT_MODEL,
    MutationKind.DROP_PROVIDER: EditOp.DROP_PROVIDER,
    MutationKind.DEMOTE: EditOp.DEMOTE,
    MutationKind.PROMOTE: EditOp.PROMOTE,
}

DEFAULT_COLD_START_PER_TIER = 1
"""How many cold-start models to trial per tier per cycle (a design parameter —
conservative so the loop probes the field gradually rather than flooding a chain)."""

DEFAULT_MAX_MUTATIONS = 2
"""Hard cap on the chain-MUTATING edits (evict / drop / demote / promote) applied
per cycle — the safety backstop. Bounds the number of distinct in-chain MODELS
mutated per cycle (not ref-level changes: a tier-agnostic edit can touch the same
model across several slots, so the worst-case blast radius is ``max_mutations``
models × the slots they appear in; the rewriter's never-empty guard still keeps
each chain's backstop). Even if attribution mis-fires, a bad verdict degrades a
bounded, observable amount per cycle rather than gutting everything at once;
combined with the volume reduction from the attribution fixes it is the floor of
safety. Cold-starts are bounded separately by ``per_tier``. A negative value is
clamped to 0 (fails closed)."""


@dataclass(frozen=True)
class ChainRewritePlan:
    """The shadow outcome of :func:`plan_chain_rewrite`.

    Attributes:
        rewrite: The :class:`RewriteResult` (new content + applied/skipped edits)
            — not written to disk here (3d-2 writes, flag-gated).
        cold_starts: The synthesized cold-start mutations, for the 3c journal
            (recorded with ``applied=False`` until 3d-2 writes).
    """

    rewrite: RewriteResult
    cold_starts: tuple[PlannedMutation, ...]


def in_chain_canonicals(content: str, equivalences: EquivalenceTable) -> frozenset[str]:
    """The canonical keys of every model currently in the four chains.

    Each chain ref's provider model id is mapped to its canonical (or kept as the
    raw id when it has no equivalence), so cold-start membership is tested in the
    same canonical space the joined profiles use.
    """
    found: set[str] = set()
    for line in content.split("\n"):
        key, separator, value = line.partition("=")
        if not separator or key not in _SLOT_KEYS:
            continue
        for ref in Chain.parse(value).refs:
            found.add(equivalences.canonical_for_model_id(ref.model) or ref.model)
    return frozenset(found)


def _chain_model_ids(content: str) -> frozenset[str]:
    """The raw provider model ids present across the four chains (for edit matching)."""
    ids: set[str] = set()
    for line in content.split("\n"):
        key, separator, value = line.partition("=")
        if not separator or key not in _SLOT_KEYS:
            continue
        for ref in Chain.parse(value).refs:
            ids.add(ref.model)
    return frozenset(ids)


def _compact(text: str) -> str:
    """Lowercase and strip non-alphanumerics (mirrors the equivalence normaliser)."""
    return "".join(char for char in text.lower() if char.isalnum())


def resolve_provider_cell(
    canonical: str,
    equivalences: EquivalenceTable,
    matrix: ProviderModelMatrix,
    healthy_cells: frozenset[tuple[str, str]],
) -> ModelCell | None:
    """A concrete matrix cell for ``canonical``, preferring a healthy one.

    Scans the live matrix for cells whose provider model id both maps to
    ``canonical`` via the equivalences AND carries the canonical's compact form
    as a substring — the second test is the specificity guard: the equivalence
    ``id_patterns`` are unanchored, so a sibling variant (``…-air`` / ``-flash``,
    the non-``speciale``) can map to the same canonical, and matching the
    canonical slug itself excludes those wrong concrete models. Among the
    survivors a healthy cell wins, then the shortest id (the base model over a
    longer-named variant), deterministically. Returns ``None`` when nothing
    matches (model unreachable — not cold-started).
    """
    target = _compact(canonical)
    matches = [
        cell
        for cell in matrix.cells
        if equivalences.canonical_for_model_id(cell.model_id) == canonical
        and target in _compact(cell.model_id)
    ]
    if not matches:
        return None
    matches.sort(
        key=lambda cell: (
            0 if (cell.provider_id, cell.model_id) in healthy_cells else 1,
            len(_compact(cell.model_id)),
            cell.model_id,
        )
    )
    return matches[0]


def _cold_start_priority(placement: Placement) -> float:
    """Rank a cold-start candidate within its tier — its tier-fit strength."""
    return float(placement.scores.get(placement.tier, 0.0))


def select_cold_starts(
    placements: Sequence[Placement],
    in_chain: frozenset[str],
    equivalences: EquivalenceTable,
    matrix: ProviderModelMatrix,
    healthy_cells: frozenset[tuple[str, str]],
    *,
    per_tier: int = DEFAULT_COLD_START_PER_TIER,
) -> tuple[tuple[ChainEdit, ...], tuple[PlannedMutation, ...]]:
    """Choose cold-start trials and render them as edits + journal mutations.

    A candidate qualifies when its canonical is absent from every chain AND the
    live matrix offers a provider cell for it that is **observed healthy** (in
    ``healthy_cells``: ≥``min_samples`` content-returning probes over the window).
    Never trialling an unproven or dead cell strongly damps the insert↔evict
    oscillation: within a single window a cell healthy enough to cold-start cannot
    also be unhealthy enough to be dropped, so the incident's pattern (cold-start
    onto a never-healthy OpenRouter cell, removed on the next sweep) is eliminated.
    A cell sitting exactly on the health boundary can still flip across adjacent
    windows, but that is a marginal cell and is rate-limited by the per-cycle cap
    (Fix-1) — not the zero-history thrash this removes. Within each tier the
    strongest-fit qualifiers are taken (up to ``per_tier``); each yields an
    ``INSERT`` edit and a cold-start journal mutation.
    """
    by_tier: dict[str, list[tuple[Placement, ModelCell]]] = {}
    for placement in placements:
        if placement.model_name in in_chain:
            continue
        cell = resolve_provider_cell(placement.model_name, equivalences, matrix, healthy_cells)
        if cell is None:
            continue
        if (cell.provider_id, cell.model_id) not in healthy_cells:
            continue
        by_tier.setdefault(placement.tier, []).append((placement, cell))

    edits: list[ChainEdit] = []
    mutations: list[PlannedMutation] = []
    for tier, candidates in by_tier.items():
        candidates.sort(key=lambda pair: _cold_start_priority(pair[0]), reverse=True)
        for placement, cell in candidates[:per_tier]:
            reason = (
                f"cold-start trial: {placement.model_name} -> {tier} "
                f"via {cell.provider_id}/{cell.model_id}, absent from chains"
            )
            edits.append(
                ChainEdit(
                    op=EditOp.INSERT,
                    model=cell.model_id,
                    provider=cell.provider_id,
                    tier=tier,
                    reason=reason,
                )
            )
            mutations.append(
                PlannedMutation(
                    kind=MutationKind.COLD_START,
                    model=cell.model_id,
                    provider=cell.provider_id,
                    metric=None,
                    reason=reason,
                )
            )
    return tuple(edits), tuple(mutations)


def mutation_to_edit(mutation: PlannedMutation) -> ChainEdit | None:
    """Map a decision-engine mutation to a structural edit (``advise`` → ``None``)."""
    op = _MUTATION_TO_EDIT_OP.get(mutation.kind)
    if op is None:
        return None
    return ChainEdit(
        op=op, model=mutation.model, provider=mutation.provider, reason=mutation.reason
    )


def plan_chain_rewrite(
    content: str,
    mutations: Sequence[PlannedMutation],
    *,
    ranking: BenchmarkRanking,
    equivalences: EquivalenceTable,
    matrix: ProviderModelMatrix,
    healthy_cells: frozenset[tuple[str, str]] = frozenset(),
    per_tier_cold_starts: int = DEFAULT_COLD_START_PER_TIER,
    max_mutations: int = DEFAULT_MAX_MUTATIONS,
) -> ChainRewritePlan:
    """Produce the shadow chains.env rewrite from mutations + cold-start trials.

    Maps the decision engine's mutations to structural edits, selects cold-start
    trials (placement on joined profiles + provider resolution), applies both
    through the mechanical rewriter, and returns the new content plus the
    synthesized cold-start mutations for journaling. Pure: nothing is written to
    disk (3d-2) and the live inputs (matrix, healthy cells, mutations) are
    injected (gathered by 3e).

    Args:
        content: The current ``chains.env`` text.
        mutations: The decision engine's planned mutations (3b).
        ranking: The benchmark prior snapshot (1c/3d-0).
        equivalences: The name↔canonical↔id resolver (1d).
        matrix: The live ``(provider × model)`` matrix (1b).
        healthy_cells: ``(provider_id, model_id)`` pairs observed healthy (2a).
        per_tier_cold_starts: Max cold-start trials per tier this cycle.
        max_mutations: Hard cap on chain-mutating edits applied this cycle (the
            safety backstop; cold-starts are bounded separately). Only edits that
            target a model actually in the chains count toward (and consume) the
            cap — no-op edits for absent models never waste the budget. A negative
            value is clamped to 0 (fails closed).

    Returns:
        The :class:`ChainRewritePlan` (shadow rewrite + cold-start mutations).
    """
    placements = place_candidates(profiles_from_ranking(ranking, equivalences=equivalences))
    in_chain = in_chain_canonicals(content, equivalences)
    cold_edits, cold_mutations = select_cold_starts(
        placements, in_chain, equivalences, matrix, healthy_cells, per_tier=per_tier_cold_starts
    )
    in_chain_ids = _chain_model_ids(content)
    mutation_edits = [
        edit
        for m in mutations
        if (edit := mutation_to_edit(m)) is not None and edit.model in in_chain_ids
    ]
    mutation_edits = mutation_edits[: max(0, max_mutations)]
    rewrite = rewrite_chains(content, [*mutation_edits, *cold_edits])
    applied = set(rewrite.applied)
    journaled = tuple(
        mutation
        for edit, mutation in zip(cold_edits, cold_mutations, strict=True)
        if edit in applied
    )
    return ChainRewritePlan(rewrite=rewrite, cold_starts=journaled)
