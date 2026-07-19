"""Tests for SP-CHAINPILOT-PLAN (Phase 3d-1c).

Covers the shadow-rewrite orchestration: in-chain canonical detection, provider
resolution (healthy-preferred), the mutation→edit mapping, cold-start selection
(absent + resolvable, per-tier), and the end-to-end plan_chain_rewrite — using
the real equivalences + benchmark prior with a synthetic live matrix.
"""

from __future__ import annotations

from collections.abc import Sequence

from repoach.llm_proxy.providers.benchmark_equivalences import load_equivalence_table
from repoach.llm_proxy.providers.benchmark_prior import load_benchmark_ranking
from repoach.llm_proxy.providers.model_matrix import ModelCell, ProviderModelMatrix
from repoach.review.chain_placement import Placement, place_candidates, profiles_from_ranking
from repoach.review.chain_plan import (
    in_chain_canonicals,
    mutation_to_edit,
    plan_chain_rewrite,
    resolve_provider_cell,
    select_cold_starts,
)
from repoach.review.chain_rewrite import EditOp
from repoach.review.decision import MutationKind, PlannedMutation

_CHAINS = """# Chain config.
MODEL_OPUS=nvidia_nim/mistralai/mistral-medium-3.5-128b,claude_code/opus

MODEL_SONNET=nvidia_nim/mistralai/mistral-medium-3.5-128b,nvidia_nim/deepseek-ai/deepseek-v4-pro,claude_code/sonnet

MODEL_HAIKU=nvidia_nim/mistralai/mistral-small-4-119b-2603,claude_code/haiku
"""


def _equiv():
    return load_equivalence_table()


def _ranking():
    return load_benchmark_ranking()


def _matrix(cells: Sequence[ModelCell]) -> ProviderModelMatrix:
    return ProviderModelMatrix(cells=tuple(cells), listings=())


def _slot(content: str, key: str) -> str:
    for line in content.split("\n"):
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    raise AssertionError(f"{key} not found")


def _placement_tier(name: str) -> str:
    placements = place_candidates(profiles_from_ranking(_ranking(), equivalences=_equiv()))
    for placement in placements:
        if placement.model_name == name:
            return placement.tier
    raise AssertionError(f"no placement for {name}")


def test_in_chain_canonicals_maps_to_canonical() -> None:
    found = in_chain_canonicals(_CHAINS, _equiv())
    assert "deepseek-v4-pro" in found
    assert "mistral-medium-3.5" in found
    assert "mistral-small-4" in found
    assert "opus" in found


def test_resolve_provider_prefers_healthy() -> None:
    cells = [
        ModelCell(provider_id="nvidia_nim", model_id="z-ai/glm-5.2"),
        ModelCell(provider_id="open_router", model_id="z-ai/glm-5.2"),
    ]
    healthy = frozenset({("open_router", "z-ai/glm-5.2")})
    cell = resolve_provider_cell("glm-5.2", _equiv(), _matrix(cells), healthy)
    assert cell is not None
    assert cell.provider_id == "open_router"


def test_resolve_provider_falls_back_then_none() -> None:
    cells = [ModelCell(provider_id="nvidia_nim", model_id="z-ai/glm-5.2")]
    cell = resolve_provider_cell("glm-5.2", _equiv(), _matrix(cells), frozenset())
    assert cell is not None
    assert cell.provider_id == "nvidia_nim"
    assert resolve_provider_cell("glm-5.2", _equiv(), _matrix([]), frozenset()) is None


def test_mutation_to_edit_maps_kinds_and_drops_advise() -> None:
    cases = {
        MutationKind.EVICT_MODEL: EditOp.EVICT_MODEL,
        MutationKind.DROP_PROVIDER: EditOp.DROP_PROVIDER,
        MutationKind.DEMOTE: EditOp.DEMOTE,
        MutationKind.PROMOTE: EditOp.PROMOTE,
    }
    for kind, op in cases.items():
        edit = mutation_to_edit(
            PlannedMutation(kind=kind, model="x/y", provider="prov", metric=None, reason="r")
        )
        assert edit is not None
        assert edit.op is op
        assert edit.model == "x/y"
        assert edit.reason == "r"

    for kind in (MutationKind.ADVISE, MutationKind.COLD_START):
        assert (
            mutation_to_edit(
                PlannedMutation(kind=kind, model="x/y", provider=None, metric=None, reason="r")
            )
            is None
        )


def test_resolve_provider_picks_specific_sibling_not_a_variant() -> None:
    cells = [
        ModelCell(provider_id="nvidia_nim", model_id="deepseek-ai/deepseek-v3.2"),
        ModelCell(provider_id="nvidia_nim", model_id="deepseek-ai/deepseek-v3.2-speciale"),
    ]
    cell = resolve_provider_cell("deepseek-v3.2-speciale", _equiv(), _matrix(cells), frozenset())
    assert cell is not None
    assert cell.model_id == "deepseek-ai/deepseek-v3.2-speciale"


def test_cold_start_not_journaled_when_insert_refused() -> None:
    plan = plan_chain_rewrite(
        _CHAINS,
        [],
        ranking=_ranking(),
        equivalences=_equiv(),
        matrix=_matrix([ModelCell(provider_id="not_a_provider", model_id="z-ai/glm-5.2")]),
    )
    assert plan.rewrite.new_content == _CHAINS
    assert plan.cold_starts == ()


def test_select_cold_starts_only_absent_resolvable() -> None:
    placements = place_candidates(profiles_from_ranking(_ranking(), equivalences=_equiv()))
    in_chain = in_chain_canonicals(_CHAINS, _equiv())
    cells = [
        ModelCell(provider_id="nvidia_nim", model_id="z-ai/glm-5.2"),
        ModelCell(provider_id="nvidia_nim", model_id="deepseek-ai/deepseek-v4-pro"),
    ]
    healthy = frozenset({("nvidia_nim", "z-ai/glm-5.2")})
    edits, mutations = select_cold_starts(placements, in_chain, _equiv(), _matrix(cells), healthy)

    inserted = {edit.model for edit in edits}
    assert "z-ai/glm-5.2" in inserted
    assert "deepseek-ai/deepseek-v4-pro" not in inserted
    assert all(edit.op is EditOp.INSERT for edit in edits)
    assert all(m.kind is MutationKind.COLD_START for m in mutations)
    assert len(edits) == len(mutations)


def test_select_cold_starts_respects_per_tier() -> None:
    placements = [
        Placement(model_name="cand-a", tier="haiku", scores={"haiku": 2.0}),
        Placement(model_name="cand-b", tier="haiku", scores={"haiku": 1.0}),
    ]
    equiv = _equiv()
    cells = [
        ModelCell(provider_id="nvidia_nim", model_id="z-ai/glm-5.2"),
        ModelCell(provider_id="open_router", model_id="z-ai/glm-5.2"),
    ]
    edits, _ = select_cold_starts(
        placements, frozenset(), equiv, _matrix(cells), frozenset(), per_tier=1
    )
    assert len(edits) <= 1


def test_select_cold_starts_skips_unhealthy_cell() -> None:
    placements = place_candidates(profiles_from_ranking(_ranking(), equivalences=_equiv()))
    in_chain = in_chain_canonicals(_CHAINS, _equiv())
    cells = [ModelCell(provider_id="nvidia_nim", model_id="z-ai/glm-5.2")]

    edits, mutations = select_cold_starts(
        placements, in_chain, _equiv(), _matrix(cells), frozenset()
    )

    assert edits == ()
    assert mutations == ()


def test_plan_chain_rewrite_inserts_resolvable_cold_start() -> None:
    cells = [ModelCell(provider_id="nvidia_nim", model_id="z-ai/glm-5.2")]
    plan = plan_chain_rewrite(
        _CHAINS,
        [],
        ranking=_ranking(),
        equivalences=_equiv(),
        matrix=_matrix(cells),
        healthy_cells=frozenset({("nvidia_nim", "z-ai/glm-5.2")}),
    )
    tier = _placement_tier("glm-5.2")
    assert "nvidia_nim/z-ai/glm-5.2" in _slot(plan.rewrite.new_content, f"MODEL_{tier.upper()}")
    assert any(m.model == "z-ai/glm-5.2" for m in plan.cold_starts)


def test_plan_chain_rewrite_noop_without_inputs() -> None:
    plan = plan_chain_rewrite(
        _CHAINS,
        [],
        ranking=_ranking(),
        equivalences=_equiv(),
        matrix=_matrix([]),
    )
    assert plan.rewrite.new_content == _CHAINS
    assert plan.cold_starts == ()


def test_plan_chain_rewrite_applies_a_mutation() -> None:
    plan = plan_chain_rewrite(
        _CHAINS,
        [
            PlannedMutation(
                kind=MutationKind.EVICT_MODEL,
                model="deepseek-ai/deepseek-v4-pro",
                provider=None,
                metric=None,
                reason="model fault",
            )
        ],
        ranking=_ranking(),
        equivalences=_equiv(),
        matrix=_matrix([]),
    )
    assert "deepseek-v4-pro" not in _slot(plan.rewrite.new_content, "MODEL_SONNET")


def test_max_mutations_caps_applied_edits() -> None:
    content = (
        "MODEL_OPUS=nvidia_nim/a/m1,nvidia_nim/a/m2,nvidia_nim/a/m3,"
        "nvidia_nim/a/m4,claude_code/opus\n"
    )
    evicts = [
        PlannedMutation(
            kind=MutationKind.EVICT_MODEL, model=f"a/m{i}", provider=None, metric=None, reason="r"
        )
        for i in range(1, 5)
    ]

    plan = plan_chain_rewrite(
        content,
        evicts,
        ranking=_ranking(),
        equivalences=_equiv(),
        matrix=_matrix([]),
        max_mutations=2,
    )

    opus = _slot(plan.rewrite.new_content, "MODEL_OPUS").split(",")
    survivors = [ref for ref in opus if not ref.startswith("claude_code/")]
    assert len(survivors) == 2
    assert len(plan.rewrite.applied) == 2


def test_max_mutations_default_caps_at_two() -> None:
    content = (
        "MODEL_OPUS=nvidia_nim/a/m1,nvidia_nim/a/m2,nvidia_nim/a/m3,"
        "nvidia_nim/a/m4,claude_code/opus\n"
    )
    evicts = [
        PlannedMutation(
            kind=MutationKind.EVICT_MODEL, model=f"a/m{i}", provider=None, metric=None, reason="r"
        )
        for i in range(1, 5)
    ]

    plan = plan_chain_rewrite(
        content, evicts, ranking=_ranking(), equivalences=_equiv(), matrix=_matrix([])
    )

    assert len(plan.rewrite.applied) == 2


def test_negative_max_mutations_fails_closed() -> None:
    content = "MODEL_OPUS=nvidia_nim/a/m1,nvidia_nim/a/m2,claude_code/opus\n"
    evicts = [
        PlannedMutation(
            kind=MutationKind.EVICT_MODEL, model=f"a/m{i}", provider=None, metric=None, reason="r"
        )
        for i in range(1, 3)
    ]

    plan = plan_chain_rewrite(
        content,
        evicts,
        ranking=_ranking(),
        equivalences=_equiv(),
        matrix=_matrix([]),
        max_mutations=-1,
    )

    assert plan.rewrite.new_content == content
    assert len(plan.rewrite.applied) == 0


def test_absent_model_edit_does_not_consume_cap() -> None:
    content = "MODEL_OPUS=nvidia_nim/a/m1,claude_code/opus\n"
    mutations = [
        PlannedMutation(
            kind=MutationKind.EVICT_MODEL,
            model="ghost/absent",
            provider=None,
            metric=None,
            reason="r",
        ),
        PlannedMutation(
            kind=MutationKind.EVICT_MODEL, model="a/m1", provider=None, metric=None, reason="r"
        ),
    ]

    plan = plan_chain_rewrite(
        content,
        mutations,
        ranking=_ranking(),
        equivalences=_equiv(),
        matrix=_matrix([]),
        max_mutations=1,
    )

    assert "a/m1" not in _slot(plan.rewrite.new_content, "MODEL_OPUS")
    assert len(plan.rewrite.applied) == 1
