"""Tests for SP-CHAINPILOT-PLACE (Phase 3d-1b).

Covers profile harvesting from the prior, the semantic-DIRECTION placement
(largest z-projection), and the edge cases: missing / zero-variance axes, the
SONNET tie default, the no-mediocre-in-OPUS guard, and empty input.
"""

from __future__ import annotations

from collections.abc import Sequence

from ferova.llm_proxy.providers.benchmark_prior import load_benchmark_ranking
from ferova.review.chain_placement import (
    TIER_DIRECTIONS,
    CandidateProfile,
    Placement,
    place_candidates,
    profiles_from_ranking,
)


def _by_name(placements: Sequence[Placement]) -> dict[str, str]:
    return {p.model_name: p.tier for p in placements}


def test_tier_directions_are_the_three_tiers() -> None:
    assert set(TIER_DIRECTIONS) == {"opus", "sonnet", "haiku"}
    for direction in TIER_DIRECTIONS.values():
        assert len(direction) == 3


def test_profiles_from_shipped_snapshot() -> None:
    ranking = load_benchmark_ranking()
    profiles = profiles_from_ranking(ranking)

    assert len(profiles) == len(ranking.model_names())
    by_name = {p.model_name: p for p in profiles}
    opus = by_name["Claude Opus 4.8 (max)"]
    assert opus.speed == 61
    assert opus.price == 3.85
    head = by_name["Mistral Medium 3.5"]
    assert head.speed == 140.7
    assert head.price == 1.16


def test_nominal_tiers_on_a_spread_population() -> None:
    profiles = [
        CandidateProfile("opus-like", quality=100, speed=10, price=50),
        CandidateProfile("sonnet-like", quality=85, speed=180, price=0.5),
        CandidateProfile("haiku-like", quality=30, speed=300, price=0.1),
        CandidateProfile("mid-a", quality=55, speed=120, price=5),
        CandidateProfile("mid-b", quality=55, speed=120, price=5),
    ]

    tiers = _by_name(place_candidates(profiles))

    assert tiers["opus-like"] == "opus"
    assert tiers["sonnet-like"] == "sonnet"
    assert tiers["haiku-like"] == "haiku"


def test_tie_resolves_to_sonnet() -> None:
    placements = place_candidates([CandidateProfile("avg", None, None, None)])
    assert placements[0].tier == "sonnet"


def test_slow_expensive_mediocre_does_not_land_in_opus() -> None:
    profiles = [
        CandidateProfile("top", quality=100, speed=200, price=0.5),
        CandidateProfile("mid", quality=60, speed=120, price=5),
        CandidateProfile("mediocre", quality=40, speed=20, price=50),
    ]
    tiers = _by_name(place_candidates(profiles))
    assert tiers["mediocre"] != "opus"
    assert tiers["mediocre"] == "sonnet"


def test_data_sparse_weak_model_defaults_to_sonnet() -> None:
    profiles = [
        CandidateProfile("a", quality=100, speed=200, price=1),
        CandidateProfile("b", quality=50, speed=100, price=3),
        CandidateProfile("sparse", quality=20, speed=None, price=None),
    ]
    tiers = _by_name(place_candidates(profiles))
    assert tiers["sparse"] == "sonnet"


def test_zero_variance_axis_does_not_crash() -> None:
    profiles = [
        CandidateProfile("x", quality=50, speed=100, price=1),
        CandidateProfile("y", quality=50, speed=100, price=1),
    ]
    placements = place_candidates(profiles)
    assert {p.tier for p in placements} <= set(TIER_DIRECTIONS)


def test_empty_input_yields_empty() -> None:
    assert place_candidates([]) == ()


def test_real_snapshot_opus_class_lands_in_opus() -> None:
    ranking = load_benchmark_ranking()
    placements = place_candidates(profiles_from_ranking(ranking))
    tiers = _by_name(placements)

    assert tiers["Claude Opus 4.8 (max)"] == "opus"
    for placement in placements:
        assert placement.tier in set(TIER_DIRECTIONS)
