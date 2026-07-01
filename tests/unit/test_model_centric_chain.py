"""Tests for SP-CHAINPILOT-CHAIN-MODEL-CENTRIC (Phase 1e).

Covers deriving the model-centric view from a flat chain (grouping + order),
the query helpers, round-trip / regroup via to_chain, the custom-key seam for
semantic grouping, and the non-empty validation.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ferova.llm_proxy.routing.chain import Chain
from ferova.llm_proxy.routing.model_centric import ModelCentricChain, ModelGroup
from ferova.llm_proxy.routing.refs import ModelRef

_INTERLEAVED = "nvidia_nim/mistralai/x,open_router/mistralai/x,nvidia_nim/qwen/y"
_SINGLE = "nvidia_nim/mistralai/a,open_router/mistralai/b,claude_code/opus"


def test_from_chain_groups_by_model_preserving_order() -> None:
    mcc = ModelCentricChain.from_chain(Chain.parse(_INTERLEAVED))

    assert len(mcc) == 2
    assert mcc.models() == ("mistralai/x", "qwen/y")
    first = mcc.providers_for("mistralai/x")
    assert tuple(ref.provider_id for ref in first) == ("nvidia_nim", "open_router")
    assert mcc.providers_for("qwen/y") == (ModelRef.parse("nvidia_nim/qwen/y"),)


def test_query_helpers() -> None:
    mcc = ModelCentricChain.from_chain(Chain.parse(_INTERLEAVED))

    assert mcc.groups[0].head() == ModelRef.parse("nvidia_nim/mistralai/x")
    assert mcc.providers_for("absent/model") == ()


def test_to_chain_regroups_interleaved() -> None:
    mcc = ModelCentricChain.from_chain(Chain.parse(_INTERLEAVED))

    flattened = mcc.to_chain()
    assert tuple(str(ref) for ref in flattened.refs) == (
        "nvidia_nim/mistralai/x",
        "open_router/mistralai/x",
        "nvidia_nim/qwen/y",
    )


def test_to_chain_round_trips_model_adjacent_chain() -> None:
    original = Chain.parse(_SINGLE)
    mcc = ModelCentricChain.from_chain(original)

    assert len(mcc) == 3
    assert mcc.to_chain() == original


def test_custom_key_collapses_distinct_models() -> None:
    chain = Chain.parse("nvidia_nim/deepseek-ai/deepseek-v4-pro,open_router/deepseek/deepseek-v4")
    mcc = ModelCentricChain.from_chain(chain, key=lambda ref: "deepseek-v4")

    assert len(mcc) == 1
    group = mcc.groups[0]
    assert group.model == "deepseek-v4"
    assert tuple(ref.provider_id for ref in group.providers) == ("nvidia_nim", "open_router")


def test_empty_structures_fail_loud() -> None:
    with pytest.raises(ValidationError):
        ModelCentricChain(groups=())
    with pytest.raises(ValidationError):
        ModelGroup(model="x", providers=())
