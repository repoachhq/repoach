"""Unit suite for model-first tier selection (SP-MFC-SELECT).

Builds small in-memory rankings and asserts the Claude-anchored eligibility,
the tier-specific top-N, and the servable gate. Pure — no I/O.
"""

from __future__ import annotations

import pytest

from ferova.llm_proxy.providers.aa_ingest import (
    AaRanking,
    ModelCapability,
    normalize_model_name,
)
from ferova.llm_proxy.routing.model_select import (
    SelectError,
    resolve_anchors,
    select_models,
)


def _cap(display: str, capability: float, *, tps: float | None = None) -> ModelCapability:
    """A ModelCapability keyed on the normalized display name."""
    return ModelCapability(
        name=normalize_model_name(display),
        capability=capability,
        coding=None,
        cheapest_input=None,
        fastest_tps=tps,
        variants=(),
    )


_ANCHORS = (
    _cap("Claude Opus 4.7", 53.5),
    _cap("Claude Sonnet 4.6", 47.2),
    _cap("Claude 4.5 Haiku", 29.6),
)


def _ranking(*extra: ModelCapability) -> AaRanking:
    """A ranking carrying the three Claude anchors plus any extra models."""
    return AaRanking(index_version="v4.1", models=(*_ANCHORS, *extra))


def _all_servable(ranking: AaRanking) -> frozenset[str]:
    """Every model in the ranking is servable."""
    return frozenset(model.name for model in ranking.models)


def test_resolve_anchors_returns_claude_capabilities() -> None:
    """The anchors are the collapsed capability of each Claude reference model."""
    anchors = resolve_anchors(_ranking())
    assert (anchors.opus, anchors.sonnet, anchors.haiku) == (53.5, 47.2, 29.6)


def test_resolve_anchors_missing_model_raises() -> None:
    """A ranking missing an anchor model fails loud."""
    partial = AaRanking(index_version=None, models=(_cap("Claude Opus 4.7", 53.5),))
    with pytest.raises(SelectError):
        resolve_anchors(partial)


def test_opus_is_top_n_by_capability_above_floor() -> None:
    """opus = top-N servable models with capability >= opus anchor - margin (48.5)."""
    ranking = _ranking(
        _cap("GPT-5.5", 54.8),
        _cap("Strong", 50.0),
        _cap("JustIn", 48.5),
        _cap("TooLow", 48.4),
    )
    result = select_models(ranking, servable=_all_servable(ranking), depth={"opus": 3})
    names = [model.name for model in result["opus"]]
    assert names == [normalize_model_name(n) for n in ("GPT-5.5", "Claude Opus 4.7", "Strong")]
    assert normalize_model_name("TooLow") not in names


def test_sonnet_band_excludes_opus_tier() -> None:
    """sonnet holds only [sonnet_floor, opus_floor) = [42.2, 48.5); no opus leak."""
    ranking = _ranking(_cap("MidA", 44.0), _cap("MidB", 43.0), _cap("Opusy", 49.0))
    result = select_models(ranking, servable=_all_servable(ranking), depth={"sonnet": 5})
    names = [model.name for model in result["sonnet"]]
    assert normalize_model_name("Claude Sonnet 4.6") in names
    assert normalize_model_name("MidA") in names
    assert normalize_model_name("Opusy") not in names
    assert normalize_model_name("Claude Opus 4.7") not in names


def test_haiku_ordered_by_speed_none_last() -> None:
    """haiku ranks by fastest_tps desc among haiku-eligible; None tps sorts last."""
    ranking = _ranking(
        _cap("Fast", 26.0, tps=200.0),
        _cap("Mid", 26.0, tps=120.0),
        _cap("NoSpeed", 27.0, tps=None),
    )
    servable = frozenset(normalize_model_name(n) for n in ("Fast", "Mid", "NoSpeed"))
    result = select_models(ranking, servable=servable, depth={"haiku": 3})
    names = [model.name for model in result["haiku"]]
    assert names == [normalize_model_name(n) for n in ("Fast", "Mid", "NoSpeed")]


def test_unservable_model_excluded_from_every_tier() -> None:
    """A high-capability model absent from the servable set wins no slot."""
    ranking = _ranking(_cap("Ghost", 60.0))
    servable = frozenset(model.name for model in _ANCHORS)
    result = select_models(ranking, servable=servable, depth={"opus": 5})
    assert normalize_model_name("Ghost") not in [m.name for m in result["opus"]]
    assert result["opus"] == (_ANCHORS[0],)


def test_short_tier_when_few_eligible_no_padding() -> None:
    """A tier with fewer eligible servable models than depth returns a shorter tuple."""
    ranking = _ranking()
    result = select_models(ranking, servable=_all_servable(ranking), depth={"opus": 5})
    assert result["opus"] == (_ANCHORS[0],)
    assert len(result["opus"]) == 1
