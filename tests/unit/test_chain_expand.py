"""Unit suite for model-first provider expansion (SP-MFC-EXPAND).

Exercises the equivalence join, NIM-first speed ordering, the claude_code tail,
and de-duplication. Pure — matrix, equivalences and speed are constructed inline.
"""

from __future__ import annotations

from ferova.llm_proxy.providers.aa_ingest import ModelCapability, normalize_model_name
from ferova.llm_proxy.providers.benchmark_equivalences import (
    EquivalenceTable,
    ModelEquivalence,
)
from ferova.llm_proxy.providers.model_matrix import ModelCell, ProviderModelMatrix
from ferova.llm_proxy.routing.chain_expand import (
    build_servable_index,
    expand_chains,
    expand_tier,
    order_cells,
    servable_names,
)


def _equivalences() -> EquivalenceTable:
    """A table mapping two benchmark names to id-pattern tokens."""
    return EquivalenceTable(
        equivalences=(
            ModelEquivalence(
                canonical="alpha", aliases=("Alpha Model",), id_patterns=("alphamodel",)
            ),
            ModelEquivalence(canonical="beta", aliases=("Beta Model",), id_patterns=("betamodel",)),
        )
    )


def _matrix(*cells: ModelCell) -> ProviderModelMatrix:
    """A matrix carrying the given cells (listings unused here)."""
    return ProviderModelMatrix(cells=tuple(cells), listings=())


def _cap(display: str) -> ModelCapability:
    """A ModelCapability keyed on the normalized display name."""
    return ModelCapability(
        name=normalize_model_name(display),
        capability=0.0,
        coding=None,
        cheapest_input=None,
        fastest_tps=None,
        variants=(),
    )


def _speed(table: dict[tuple[str, str], float | None]):
    """A speed_for callable backed by a dict (missing → None)."""

    def speed_for(provider_id: str, model_id: str) -> float | None:
        return table.get((provider_id, model_id))

    return speed_for


def test_index_maps_cells_to_alias_names_and_drops_unmatched() -> None:
    """A cell attaches under its alias's normalized name; an unmatched cell drops."""
    matrix = _matrix(
        ModelCell("nvidia_nim", "x/alpha-model"),
        ModelCell("deepseek", "z/beta-model"),
        ModelCell("nvidia_nim", "q/unknown-thing"),
    )
    index = build_servable_index(matrix, _equivalences())
    assert servable_names(index) == frozenset({"alphamodel", "betamodel"})
    flat = [cell.model_id for cells in index.values() for cell in cells]
    assert "q/unknown-thing" not in flat


def test_order_cells_nim_first_then_latency_none_last() -> None:
    """NIM heads the block; the rest order by ascending latency, None last."""
    cells = [
        ModelCell("open_router", "or/alpha-model"),
        ModelCell("groq", "gq/alpha-model"),
        ModelCell("nvidia_nim", "nim/alpha-model"),
        ModelCell("deepseek", "ds/alpha-model"),
    ]
    speed = _speed(
        {
            ("open_router", "or/alpha-model"): 2.0,
            ("deepseek", "ds/alpha-model"): 1.0,
            ("nvidia_nim", "nim/alpha-model"): 9.0,
            ("groq", "gq/alpha-model"): None,
        }
    )
    ordered = order_cells(cells, speed_for=speed)
    assert [cell.provider_id for cell in ordered] == [
        "nvidia_nim",
        "deepseek",
        "open_router",
        "groq",
    ]


def test_expand_tier_appends_claude_code_tail_and_skips_missing() -> None:
    """Selected models expand to provider/model entries, then the tail; missing skipped."""
    matrix = _matrix(
        ModelCell("nvidia_nim", "nim/alpha-model"),
        ModelCell("open_router", "or/alpha-model"),
    )
    index = build_servable_index(matrix, _equivalences())
    speed = _speed({("open_router", "or/alpha-model"): 1.0})
    models = (_cap("Alpha Model"), _cap("Missing Model"))
    chain = expand_tier(models, index=index, speed_for=speed, tier="opus")
    assert chain == (
        "nvidia_nim/nim/alpha-model",
        "open_router/or/alpha-model",
        "claude_code/opus",
    )


def test_expand_tier_dedupes_shared_cell() -> None:
    """A provider/model reachable via two selected models is emitted once."""
    matrix = _matrix(ModelCell("nvidia_nim", "nim/alpha-model"))
    eqs = EquivalenceTable(
        equivalences=(
            ModelEquivalence(
                canonical="alpha",
                aliases=("Alpha Model", "Alias Two"),
                id_patterns=("alphamodel",),
            ),
        )
    )
    index = build_servable_index(matrix, eqs)
    speed = _speed({})
    models = (_cap("Alpha Model"), _cap("Alias Two"))
    chain = expand_tier(models, index=index, speed_for=speed, tier="haiku")
    assert chain == ("nvidia_nim/nim/alpha-model", "claude_code/haiku")


def test_expand_chains_one_tuple_per_tier() -> None:
    """expand_chains returns one ordered chain per tier, each with its tail."""
    matrix = _matrix(ModelCell("nvidia_nim", "nim/alpha-model"))
    index = build_servable_index(matrix, _equivalences())
    speed = _speed({})
    selections = {"opus": (_cap("Alpha Model"),), "sonnet": (), "haiku": (_cap("Alpha Model"),)}
    chains = expand_chains(selections, index=index, speed_for=speed)
    assert chains["opus"] == ("nvidia_nim/nim/alpha-model", "claude_code/opus")
    assert chains["sonnet"] == ("claude_code/sonnet",)
    assert chains["haiku"] == ("nvidia_nim/nim/alpha-model", "claude_code/haiku")


def _non_nim_head_index() -> dict:
    """A servable index where the top model (alpha) is non-NIM and beta is NIM."""
    matrix = _matrix(
        ModelCell("open_router", "or/alpha-model"),
        ModelCell("deepseek", "ds/alpha-model"),
        ModelCell("nvidia_nim", "nim/beta-model"),
    )
    return build_servable_index(matrix, _equivalences())


def test_expand_tier_promotes_nim_head_for_sonnet() -> None:
    """A NIM-head tier whose capability head is paid promotes the first NIM cell."""
    index = _non_nim_head_index()
    speed = _speed({("open_router", "or/alpha-model"): 1.0, ("deepseek", "ds/alpha-model"): 2.0})
    models = (_cap("Alpha Model"), _cap("Beta Model"))
    chain = expand_tier(models, index=index, speed_for=speed, tier="sonnet")
    assert chain == (
        "nvidia_nim/nim/beta-model",
        "open_router/or/alpha-model",
        "deepseek/ds/alpha-model",
        "claude_code/sonnet",
    )


def test_expand_tier_opus_keeps_non_nim_head() -> None:
    """opus is exempt — its non-NIM capability head is never reordered."""
    index = _non_nim_head_index()
    speed = _speed({("open_router", "or/alpha-model"): 1.0, ("deepseek", "ds/alpha-model"): 2.0})
    models = (_cap("Alpha Model"), _cap("Beta Model"))
    chain = expand_tier(models, index=index, speed_for=speed, tier="opus")
    assert chain[0] == "open_router/or/alpha-model"
    assert chain[-1] == "claude_code/opus"


def test_expand_tier_nim_head_noop_when_already_nim() -> None:
    """A NIM-head tier already led by NIM is left unchanged."""
    matrix = _matrix(
        ModelCell("nvidia_nim", "nim/alpha-model"),
        ModelCell("open_router", "or/alpha-model"),
    )
    index = build_servable_index(matrix, _equivalences())
    speed = _speed({("open_router", "or/alpha-model"): 1.0})
    chain = expand_tier((_cap("Alpha Model"),), index=index, speed_for=speed, tier="haiku")
    assert chain == (
        "nvidia_nim/nim/alpha-model",
        "open_router/or/alpha-model",
        "claude_code/haiku",
    )


def test_expand_tier_no_nim_cell_keeps_capability_head() -> None:
    """A NIM-head tier with no NIM cell keeps the capability head (degrade, not crash)."""
    matrix = _matrix(ModelCell("open_router", "or/alpha-model"))
    index = build_servable_index(matrix, _equivalences())
    speed = _speed({("open_router", "or/alpha-model"): 1.0})
    chain = expand_tier((_cap("Alpha Model"),), index=index, speed_for=speed, tier="sonnet")
    assert chain == ("open_router/or/alpha-model", "claude_code/sonnet")


def test_expand_chains_threads_nim_head_policy_by_default() -> None:
    """expand_chains applies the NIM-head policy to sonnet/haiku, not opus, by default."""
    index = _non_nim_head_index()
    speed = _speed({("open_router", "or/alpha-model"): 1.0, ("deepseek", "ds/alpha-model"): 2.0})
    models = (_cap("Alpha Model"), _cap("Beta Model"))
    chains = expand_chains(
        {"opus": models, "sonnet": models, "haiku": models}, index=index, speed_for=speed
    )
    assert chains["opus"][0] == "open_router/or/alpha-model"
    assert chains["sonnet"][0] == "nvidia_nim/nim/beta-model"
    assert chains["haiku"][0] == "nvidia_nim/nim/beta-model"
