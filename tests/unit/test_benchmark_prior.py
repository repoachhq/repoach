"""Tests for SP-CHAINPILOT-BENCHMARK-INGEST (Phase 1c).

Covers the pure parser/query API with an inline fixture, the fail-loud
validation on a malformed payload, and that the shipped real snapshot loads and
carries provenance.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from repoach.llm_proxy.providers.benchmark_prior import (
    BenchmarkRanking,
    load_benchmark_ranking,
    parse_benchmark_ranking,
)

_FIXTURE = {
    "sources": [
        {
            "source": "artificial_analysis",
            "url": "https://artificialanalysis.ai/",
            "captured": "2026-06-23",
            "metric": "intelligence_index",
            "scale": "0-100 higher better",
        },
        {
            "source": "aider_polyglot",
            "url": "https://aider.chat/docs/leaderboards/",
            "captured": "2026-06-23",
            "metric": "pass_rate",
            "scale": "0-100 percent",
        },
    ],
    "entries": [
        {
            "source": "artificial_analysis",
            "model_name": "DeepSeek V4",
            "metric": "intelligence_index",
            "score": 71.0,
            "rank": 1,
        },
        {
            "source": "artificial_analysis",
            "model_name": "Qwen3 Coder",
            "metric": "intelligence_index",
            "score": 64.0,
            "rank": 2,
        },
        {
            "source": "aider_polyglot",
            "model_name": "DeepSeek V4",
            "metric": "pass_rate",
            "score": 82.5,
            "rank": 1,
        },
    ],
}


def test_parse_round_trips_sources_and_entries() -> None:
    ranking = parse_benchmark_ranking(_FIXTURE)

    assert isinstance(ranking, BenchmarkRanking)
    assert len(ranking.sources) == 2
    assert len(ranking.entries) == 3
    assert ranking.model_names() == ("DeepSeek V4", "Qwen3 Coder")


def test_entries_for_model_spans_sources() -> None:
    ranking = parse_benchmark_ranking(_FIXTURE)

    deepseek = ranking.entries_for_model("DeepSeek V4")
    assert {entry.source for entry in deepseek} == {"artificial_analysis", "aider_polyglot"}
    assert ranking.entries_for_model("nonexistent") == ()


def test_by_metric_and_by_source_filter() -> None:
    ranking = parse_benchmark_ranking(_FIXTURE)

    assert len(ranking.by_metric("intelligence_index")) == 2
    assert len(ranking.by_source("aider_polyglot")) == 1
    assert ranking.by_metric("unknown") == ()
    assert ranking.by_source("unknown") == ()


def test_malformed_payload_fails_loud() -> None:
    broken = {
        "sources": [],
        "entries": [{"source": "x", "model_name": "y", "metric": "m", "rank": 1}],
    }
    with pytest.raises(ValidationError):
        parse_benchmark_ranking(broken)


def test_shipped_snapshot_loads_with_provenance() -> None:
    ranking = load_benchmark_ranking()

    assert len(ranking.sources) >= 2
    assert len(ranking.entries) > 0
    for meta in ranking.sources:
        assert meta.url.startswith("http")
        assert meta.captured
    sources_with_entries = {entry.source for entry in ranking.entries}
    declared_sources = {meta.source for meta in ranking.sources}
    assert sources_with_entries <= declared_sources


def test_rank_optional_for_attribute_metrics() -> None:
    payload = {
        "sources": [
            {
                "source": "aa_speed",
                "url": "https://artificialanalysis.ai/",
                "captured": "2026-06-26",
                "metric": "output_speed",
                "scale": "tok/s higher better",
            }
        ],
        "entries": [
            {
                "source": "aa_speed",
                "model_name": "Mistral Medium 3.5",
                "metric": "output_speed",
                "score": 140.7,
            }
        ],
    }

    ranking = parse_benchmark_ranking(payload)

    (entry,) = ranking.entries
    assert entry.rank is None
    assert entry.score == 140.7


def test_shipped_snapshot_carries_speed_and_price() -> None:
    ranking = load_benchmark_ranking()

    speed = ranking.by_metric("output_speed")
    price = ranking.by_metric("price_blended")
    assert speed and price
    for entry in (*speed, *price):
        assert entry.rank is None
        assert entry.score > 0

    chain_heads = (
        "Mistral Medium 3.5",
        "Mistral Small 4 (Reasoning)",
        "DeepSeek V4 Pro (Max)",
    )
    speed_models = {entry.model_name for entry in speed}
    price_models = {entry.model_name for entry in price}
    for head in chain_heads:
        assert head in speed_models
        assert head in price_models
