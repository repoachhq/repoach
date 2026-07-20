"""SP-CHAINPILOT-PLACE — the tier placement classifier (Phase 3d-1b).

The *brain* of cold-start placement. Given the benchmark prior, it decides which
capability tier (``opus`` / ``sonnet`` / ``haiku``) a model belongs in from its
``(quality, speed, price)`` profile. Pure: it classifies, 3d-1c gathers
candidates / resolves providers / writes, 3d-1a does the mechanics.

The three tiers are told apart by **explicit semantic anchors** (operator's call,
2026-06-26) — deliberate design parameters, not a proximity to current chain
members (rejected: OPUS and SONNET share the head ``mistral-medium-3.5``, so the
members carry no OPUS/SONNET signal). Each axis is **z-scored** over the
population (more robust than min-max to the very different dispersions of an
intelligence rank vs tokens/sec vs $/Mtok).

An anchor is a **priority DIRECTION**, not a point (a fixed point would mean
"prefer the population mean" on a zeroed axis, not "indifferent" — the flaw an
earlier point-anchor cut exhibited). A model is assigned to the tier whose
direction its z-vector most strongly aligns with (the largest projection):

- OPUS = ``(quality+, speed 0, price 0)`` — maximise quality, **indifferent** to
  speed/price (zero weight, true indifference);
- SONNET = ``(quality+, speed+, price-)`` — good quality AND fast AND cheap;
- HAIKU = ``(speed+, price-)`` — fastest + cheapest, quality irrelevant.

A tier wins only on **positive** alignment: a model below-average on a tier's
priorities is never dumped there by being merely "least negative" (the failure
mode of a plain argmax — it would make OPUS a slow/expensive basin and HAIKU a
data-sparse one). OPUS additionally requires positive quality
(``quality_z > 0``) — it must never catch a mediocre model by accident. With no
positive alignment, or on a tie, the model defaults to SONNET (the balanced
workhorse, the safe cold-start default).
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from repoach.llm_proxy.providers.benchmark_equivalences import EquivalenceTable
from repoach.llm_proxy.providers.benchmark_prior import BenchmarkRanking

_QUALITY_METRIC = "intelligence_index"
_SPEED_METRIC = "output_speed"
_PRICE_METRIC = "price_blended"

TIER_DIRECTIONS: dict[str, tuple[float, float, float]] = {
    "opus": (1.0, 0.0, 0.0),
    "sonnet": (1.0, 1.0, -1.0),
    "haiku": (0.0, 1.0, -1.0),
}
"""Per-tier priority directions in z-scored ``(quality, speed, price)``.

Design parameters encoding the tier semantics: OPUS maximises quality and is
indifferent to speed/price (zero weight); SONNET wants quality AND speed AND
cheapness; HAIKU wants speed AND cheapness, quality irrelevant. A model is placed
at the tier whose direction its z-vector best aligns with (largest projection).
"""

_TIE_ORDER = ("sonnet", "haiku", "opus")
"""Tie-break precedence among the tiers — SONNET (then the cheaper tiers)
first, so a borderline cold-start defaults to the safe workhorse, never to the
costly OPUS chain by accident."""

_TIE_EPS = 1e-9


@dataclass(frozen=True)
class CandidateProfile:
    """A model's raw benchmark profile across the placement axes.

    Attributes:
        model_name: The benchmark model name (verbatim from the prior).
        quality: The ``intelligence_index`` score, or ``None`` if unranked.
        speed: The ``output_speed`` tok/s, or ``None``.
        price: The ``price_blended`` $/Mtok, or ``None``.
    """

    model_name: str
    quality: float | None
    speed: float | None
    price: float | None


@dataclass(frozen=True)
class Placement:
    """A model's assigned tier with the evidence behind it.

    Attributes:
        model_name: The benchmark model name.
        tier: The assigned tier.
        scores: The alignment projection onto each tier direction (for
            transparency / journaling).
    """

    model_name: str
    tier: str
    scores: dict[str, float]


def profiles_from_ranking(
    ranking: BenchmarkRanking, *, equivalences: EquivalenceTable | None = None
) -> tuple[CandidateProfile, ...]:
    """Harvest one :class:`CandidateProfile` per model in the ranking.

    With ``equivalences`` the prior's per-source name fragments are **joined**
    under their canonical key first (so a model's quality, speed, and price
    entries carried under differently-named fragments sit on one profile); the
    profile's ``model_name`` is then the canonical key. Without it, each raw
    model name is its own profile (the standalone behaviour).

    Args:
        ranking: The benchmark prior snapshot.
        equivalences: Optional name↔canonical resolver to collapse fragments.

    Returns:
        A profile per distinct model (or canonical), in first-seen order.
    """
    groups: dict[str, list[str]] = {}
    for model_name in ranking.model_names():
        key = model_name
        if equivalences is not None:
            key = equivalences.canonical_for_alias(model_name) or model_name
        groups.setdefault(key, []).append(model_name)

    profiles: list[CandidateProfile] = []
    for key, names in groups.items():
        quality = speed = price = None
        for name in names:
            for entry in ranking.entries_for_model(name):
                if entry.metric == _QUALITY_METRIC and quality is None:
                    quality = float(entry.score)
                elif entry.metric == _SPEED_METRIC and speed is None:
                    speed = float(entry.score)
                elif entry.metric == _PRICE_METRIC and price is None:
                    price = float(entry.score)
        profiles.append(CandidateProfile(model_name=key, quality=quality, speed=speed, price=price))
    return tuple(profiles)


def _stats(values: list[float]) -> tuple[float, float]:
    """Population ``(mean, std)`` of ``values``; ``std`` is ``0`` below two values."""
    if not values:
        return 0.0, 0.0
    mean = statistics.fmean(values)
    std = statistics.pstdev(values) if len(values) > 1 else 0.0
    return mean, std


def _z(value: float | None, mean: float, std: float) -> float:
    """Z-score, neutral ``0`` for a missing value or a zero-variance axis."""
    if value is None or std == 0.0:
        return 0.0
    return (value - mean) / std


def _project(vector: tuple[float, float, float], direction: tuple[float, float, float]) -> float:
    """The signed projection of ``vector`` onto the unit of ``direction``."""
    norm = sum(component**2 for component in direction) ** 0.5
    if norm == 0.0:
        return 0.0
    return float(sum(v * d for v, d in zip(vector, direction, strict=True)) / norm)


def _pick_general(scores: dict[str, float], z_quality: float) -> str:
    """The best positively-aligned tier, else SONNET.

    A tier wins only when the model genuinely expresses its priority (a positive
    projection), so a below-average model is never dumped into a tier merely by
    being "least negative" (the failure mode of a plain argmax). OPUS
    additionally requires positive quality (``z_quality > 0``): it is the costly
    premium chain and must never catch a mediocre model by accident. With no
    positive alignment the model defaults to SONNET, the balanced workhorse;
    ties among the positively-aligned tiers resolve by :data:`_TIE_ORDER`.
    """
    eligible = {
        tier: score
        for tier, score in scores.items()
        if score > _TIE_EPS and (tier != "opus" or z_quality > 0.0)
    }
    if not eligible:
        return "sonnet"
    best = max(eligible.values())
    for tier in _TIE_ORDER:
        if tier in eligible and abs(eligible[tier] - best) <= _TIE_EPS:
            return tier
    return "sonnet"


def place_candidates(profiles: Sequence[CandidateProfile]) -> tuple[Placement, ...]:
    """Assign each profile to a tier by the semantic-direction policy.

    Each axis is z-scored over ``profiles`` (a missing value or a zero-variance
    axis contributes ``0``). A model is placed at the tier whose
    :data:`TIER_DIRECTIONS` direction its ``(quality, speed, price)`` z-vector
    best aligns with, ties resolving to SONNET.

    Args:
        profiles: The population to classify (z-scoring is over this set, so the
            caller passes the full benchmark population — a model's tier must not
            depend on which subset is placed alongside it).

    Returns:
        One :class:`Placement` per profile, in input order.
    """
    quality_stats = _stats([p.quality for p in profiles if p.quality is not None])
    speed_stats = _stats([p.speed for p in profiles if p.speed is not None])
    price_stats = _stats([p.price for p in profiles if p.price is not None])

    placements: list[Placement] = []
    for profile in profiles:
        z_quality = _z(profile.quality, *quality_stats)
        general = (z_quality, _z(profile.speed, *speed_stats), _z(profile.price, *price_stats))
        scores = {tier: _project(general, direction) for tier, direction in TIER_DIRECTIONS.items()}
        tier = _pick_general(scores, z_quality)
        placements.append(Placement(model_name=profile.model_name, tier=tier, scores=scores))
    return tuple(placements)
