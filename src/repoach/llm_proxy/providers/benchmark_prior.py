"""Benchmark prior — ingest a versioned public-ranking snapshot (SP-CHAINPILOT-BENCHMARK-INGEST).

Phase 1c of the Chain Autopilot arc. Loads a hand-curated, version-controlled
JSON snapshot of real public benchmark rankings (``benchmark_prior.json``,
beside this module) into a queryable in-memory prior.

By Principle 3 this is only the *prior* — a starting quality estimate for a
model the factory has not yet run, which the live performance harvest (2b–2d)
later overrides. The snapshot mixes several real sources (a general-intelligence
index plus a coding leaderboard), each carrying its own provenance (url,
capture date, metric, scale); no figure is synthesised. Rankings are keyed by
each source's own model *name* — bridging those to our provider model IDs is
1d (``SP-CHAINPILOT-EQUIVALENCES``), kept separate.

The resource is trusted: a parse/validation error means a broken commit, so
loading fails loud rather than degrading.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

_RESOURCE = Path(__file__).parent / "benchmark_prior.json"

__all__ = [
    "BenchmarkEntry",
    "BenchmarkRanking",
    "BenchmarkSourceMeta",
    "load_benchmark_ranking",
    "parse_benchmark_ranking",
]


class BenchmarkEntry(BaseModel):
    """One model's value on one source's metric.

    Attributes:
        source: The source key (matches a :class:`BenchmarkSourceMeta`).
        model_name: The model name verbatim as the source lists it.
        metric: The metric this score is on (e.g. ``intelligence_index``).
        score: The numeric value on the source's scale.
        rank: The model's 1-based position in that source's ranking, or ``None``
            for raw per-model attribute metrics (e.g. ``output_speed`` /
            ``price_blended``) where the captured value is the datum and no
            global leaderboard position was read.
    """

    model_config = ConfigDict(frozen=True)

    source: str
    model_name: str
    metric: str
    score: float
    rank: int | None = None


class BenchmarkSourceMeta(BaseModel):
    """Provenance for one source block of the snapshot.

    Attributes:
        source: The source key referenced by each :class:`BenchmarkEntry`.
        url: The exact page the snapshot was captured from.
        captured: The capture date (``YYYY-MM-DD``).
        metric: The metric name the source's scores are on.
        scale: A short human description of the scale (e.g. ``0-100 higher better``).
    """

    model_config = ConfigDict(frozen=True)

    source: str
    url: str
    captured: str
    metric: str
    scale: str


class BenchmarkRanking(BaseModel):
    """A parsed benchmark-prior snapshot: provenance plus the flat entries."""

    model_config = ConfigDict(frozen=True)

    sources: tuple[BenchmarkSourceMeta, ...]
    entries: tuple[BenchmarkEntry, ...]

    def entries_for_model(self, model_name: str) -> tuple[BenchmarkEntry, ...]:
        """Every source's entry for ``model_name`` (empty if unseen)."""
        return tuple(entry for entry in self.entries if entry.model_name == model_name)

    def by_source(self, source: str) -> tuple[BenchmarkEntry, ...]:
        """The entries contributed by ``source`` (empty if unknown)."""
        return tuple(entry for entry in self.entries if entry.source == source)

    def by_metric(self, metric: str) -> tuple[BenchmarkEntry, ...]:
        """The entries scored on ``metric`` (empty if none)."""
        return tuple(entry for entry in self.entries if entry.metric == metric)

    def model_names(self) -> tuple[str, ...]:
        """The distinct model names in the snapshot, in first-seen order."""
        seen: dict[str, None] = {}
        for entry in self.entries:
            seen.setdefault(entry.model_name, None)
        return tuple(seen)


def parse_benchmark_ranking(payload: dict) -> BenchmarkRanking:
    """Validate a snapshot payload into a :class:`BenchmarkRanking`.

    Args:
        payload: The decoded JSON, ``{"sources": [...], "entries": [...]}``.

    Returns:
        The validated ranking.

    Raises:
        pydantic.ValidationError: If the payload violates the contract.
    """
    return BenchmarkRanking.model_validate(payload)


def load_benchmark_ranking(path: Path | None = None) -> BenchmarkRanking:
    """Load and validate the versioned benchmark-prior snapshot.

    Args:
        path: The JSON resource to read; defaults to ``benchmark_prior.json``
            beside this module.

    Returns:
        The validated ranking.

    Raises:
        json.JSONDecodeError: If the resource is not valid JSON.
        pydantic.ValidationError: If the resource violates the contract.
    """
    text = (path or _RESOURCE).read_text(encoding="utf-8")
    return parse_benchmark_ranking(json.loads(text))
