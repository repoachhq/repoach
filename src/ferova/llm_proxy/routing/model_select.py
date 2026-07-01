"""Model-first tier selection — Claude-anchored eligibility + top-N (SP-MFC-SELECT).

Slice 2 of the model-first chains arc (``docs/model_first_chains_architecture.md``).
Turns the collapsed capability ranking from ``SP-MFC-AA-INGEST`` into the ordered
per-tier model lists that drive each chain.

The tier bands are anchored on the Claude reference models (the ``claude_code``
chain tail): a model is ``<tier>``-eligible when its capability sits within a
margin of that tier's Claude anchor. Anchoring on Claude — rather than absolute
thresholds — keeps the bands self-evolving as the index re-baselines. Selection
is tier-specific: opus and sonnet rank by intelligence, haiku ranks by speed
("fast but not dumb"). Every tier is gated to the **servable** models first, so
an unservable global flagship never consumes a slot (matrix-before-top-N).

Pure and I/O-free: the servable set is an injected value (the matrix ∩ AA join
lives downstream in ``SP-MFC-EXPAND``), so this leaf imports neither the provider
matrix nor the equivalence table and stays trivially testable.
"""

from __future__ import annotations

from collections.abc import Container, Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from ferova.core.logging import get_logger
from ferova.llm_proxy.providers.aa_ingest import (
    AaRanking,
    ModelCapability,
    normalize_model_name,
)

_log = get_logger(__name__)

DEFAULT_MARGIN = 5.0
DEFAULT_DEPTH: dict[str, int] = {"opus": 5, "sonnet": 4, "haiku": 3}
CLAUDE_ANCHORS: dict[str, str] = {
    "opus": "Claude Opus 4.7",
    "sonnet": "Claude Sonnet 4.6",
    "haiku": "Claude 4.5 Haiku",
}
TIERS: tuple[str, ...] = ("opus", "sonnet", "haiku")

__all__ = [
    "CLAUDE_ANCHORS",
    "DEFAULT_DEPTH",
    "DEFAULT_MARGIN",
    "TIERS",
    "SelectError",
    "TierAnchors",
    "resolve_anchors",
    "select_models",
]


class SelectError(Exception):
    """Raised when selection cannot proceed — an anchor model is absent."""


class TierAnchors(BaseModel):
    """The three tiers' anchor capabilities, resolved from the Claude models.

    Attributes:
        opus: The opus anchor — Claude Opus reference model capability.
        sonnet: The sonnet anchor — Claude Sonnet reference model capability.
        haiku: The haiku anchor — Claude Haiku reference model capability.
    """

    model_config = ConfigDict(frozen=True)

    opus: float
    sonnet: float
    haiku: float


def _capability_by_name(ranking: AaRanking) -> dict[str, ModelCapability]:
    """Index the ranking's models by their normalized name."""
    return {model.name: model for model in ranking.models}


def resolve_anchors(
    ranking: AaRanking,
    *,
    names: Mapping[str, str] = CLAUDE_ANCHORS,
) -> TierAnchors:
    """Resolve the per-tier anchor capabilities from the Claude reference models.

    Args:
        ranking: The collapsed capability ranking (slice 1).
        names: The tier → Claude display-name map; normalized before lookup.

    Returns:
        The :class:`TierAnchors` carrying each tier's Claude-model capability.

    Raises:
        SelectError: When a configured anchor model is not present in the ranking.
    """
    by_name = _capability_by_name(ranking)
    resolved: dict[str, float] = {}
    for tier in TIERS:
        key = normalize_model_name(names[tier])
        model = by_name.get(key)
        if model is None:
            raise SelectError(f"anchor model {names[tier]!r} for tier {tier!r} absent from ranking")
        resolved[tier] = model.capability
    return TierAnchors(opus=resolved["opus"], sonnet=resolved["sonnet"], haiku=resolved["haiku"])


def _servable_models(ranking: AaRanking, servable: Container[str]) -> tuple[ModelCapability, ...]:
    """The ranking's models restricted to the servable set, capability-sorted."""
    return tuple(model for model in ranking.models if model.name in servable)


def _by_capability(model: ModelCapability) -> float:
    """Sort key: the model's collapsed capability."""
    return model.capability


def _by_speed(model: ModelCapability) -> tuple[int, float]:
    """Sort key for haiku: measured speed desc, unmeasured (None) last.

    Returns a ``(has_tps, tps)`` tuple so models without a measured
    ``fastest_tps`` sort after every measured one under a descending sort.
    """
    if model.fastest_tps is None:
        return (0, 0.0)
    return (1, model.fastest_tps)


def _top_n(models: Sequence[ModelCapability], key, depth: int) -> tuple[ModelCapability, ...]:
    """Take the top ``depth`` models by ``key`` (descending), deterministically."""
    ranked = sorted(models, key=key, reverse=True)
    return tuple(ranked[: max(0, depth)])


def select_models(
    ranking: AaRanking,
    *,
    servable: Container[str],
    anchors: TierAnchors | None = None,
    margin: float = DEFAULT_MARGIN,
    depth: Mapping[str, int] = DEFAULT_DEPTH,
) -> dict[str, tuple[ModelCapability, ...]]:
    """Select the ordered per-tier model lists from the ranking.

    Args:
        ranking: The collapsed capability ranking (slice 1).
        servable: The normalized names that at least one provider serves
            (matrix ∩ AA, computed by the caller — matrix-before-top-N).
        anchors: Pre-resolved anchors; resolved from ``ranking`` when ``None``.
        margin: Index points of tolerance below each Claude anchor.
        depth: The per-tier chain depth (number of models selected).

    Returns:
        ``{"opus": (...), "sonnet": (...), "haiku": (...)}``, each ordered
        head→tail by the tier's sort and bounded by its depth.

    Raises:
        SelectError: When an anchor model is absent from the ranking.
    """
    resolved = anchors or resolve_anchors(ranking)
    opus_floor = resolved.opus - margin
    sonnet_floor = resolved.sonnet - margin
    haiku_floor = resolved.haiku - margin
    pool = _servable_models(ranking, servable)

    opus = _top_n(
        [model for model in pool if model.capability >= opus_floor],
        _by_capability,
        depth.get("opus", 0),
    )
    sonnet = _top_n(
        [model for model in pool if sonnet_floor <= model.capability < opus_floor],
        _by_capability,
        depth.get("sonnet", 0),
    )
    haiku = _top_n(
        [model for model in pool if model.capability >= haiku_floor],
        _by_speed,
        depth.get("haiku", 0),
    )
    _log.info(
        "mfc_select",
        opus=len(opus),
        sonnet=len(sonnet),
        haiku=len(haiku),
        margin=margin,
    )
    return {"opus": opus, "sonnet": sonnet, "haiku": haiku}
