"""Unit suite for the Artificial Analysis free-API ingest (SP-MFC-AA-INGEST).

Drives the pure parser with captured page payloads and the fetcher with an
injected callable, so nothing here performs live network I/O.
"""

from __future__ import annotations

import pytest

from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.aa_ingest import (
    AaIngestError,
    fetch_aa_ranking,
    normalize_model_name,
    parse_aa_models,
)


def _row(
    name: str,
    slug: str,
    intelligence: float | None,
    *,
    coding: float | None = None,
    price_in: float | None = None,
    tps: float | None = None,
) -> dict:
    """Build one Artificial Analysis ``data[]`` row for a fixture."""
    return {
        "name": name,
        "slug": slug,
        "model_creator": {"name": "Acme"},
        "evaluations": {
            "artificial_analysis_intelligence_index": intelligence,
            "artificial_analysis_coding_index": coding,
            "artificial_analysis_agentic_index": None,
        },
        "pricing": {"price_1m_input_tokens": price_in, "price_1m_output_tokens": None},
        "performance": {
            "median_output_tokens_per_second": tps,
            "median_time_to_first_token_seconds": None,
        },
    }


def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """A settings instance with the key set via env and no env-file pollution."""
    monkeypatch.setenv("REPOACH_ARTIFICIAL_ANALYSIS_API_KEY", "test-key")
    return Settings(_env_file=None)


def test_normalize_strips_variant_and_punctuation() -> None:
    """The collapse key drops the variant parenthetical and non-alphanumerics."""
    assert normalize_model_name("Claude Opus 4.7 (Non-reasoning, High Effort)") == (
        normalize_model_name("Claude Opus 4.7 (Adaptive Reasoning, Max Effort)")
    )
    assert normalize_model_name("Claude Opus 4.7 (Reasoning)") == "claudeopus47"


def test_paginated_walk_returns_one_model_per_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_aa_ranking walks every page and emits one model per normalized name."""
    pages = {
        1: {
            "intelligence_index_version": "v4.1",
            "pagination": {"page": 1, "page_size": 200, "total_pages": 2},
            "data": [_row("Alpha", "alpha", 50.0), _row("Beta", "beta", 40.0)],
        },
        2: {
            "pagination": {"page": 2, "page_size": 200, "total_pages": 2},
            "data": [_row("Gamma", "gamma", 30.0)],
        },
    }
    ranking = fetch_aa_ranking(_settings(monkeypatch), fetch_page=lambda page: pages[page])
    assert ranking.index_version == "v4.1"
    assert tuple(model.name for model in ranking.models) == ("alpha", "beta", "gamma")


def test_variants_collapse_to_max_intelligence() -> None:
    """Reasoning and non-reasoning rows of a model collapse to their MAX index."""
    page = {
        "pagination": {"total_pages": 1},
        "data": [
            _row("Claude Opus 4.7 (Non-reasoning, High Effort)", "claude-opus-4-7", 42.7),
            _row("Claude Opus 4.7 (Adaptive Reasoning, Max Effort)", "claude-opus-4-7-r", 53.5),
        ],
    }
    ranking = parse_aa_models([page])
    assert len(ranking.models) == 1
    collapsed = ranking.models[0]
    assert collapsed.capability == 53.5
    assert len(collapsed.variants) == 2


def test_null_intelligence_skipped_and_all_null_coding_is_none() -> None:
    """A null-index row is dropped; a model with no coding index yields None."""
    page = {
        "pagination": {"total_pages": 1},
        "data": [
            _row("Ghost", "ghost", None),
            _row("Solid", "solid", 33.0, coding=None),
        ],
    }
    ranking = parse_aa_models([page])
    assert tuple(model.name for model in ranking.models) == ("solid",)
    assert ranking.models[0].coding is None


def test_coding_collapses_to_max_ignoring_null() -> None:
    """coding is the MAX across variants that measured it, ignoring nulls."""
    page = {
        "pagination": {"total_pages": 1},
        "data": [
            _row("Dup (Reasoning)", "dup-r", 40.0, coding=35.0),
            _row("Dup (Non-reasoning)", "dup", 38.0, coding=None),
        ],
    }
    ranking = parse_aa_models([page])
    assert ranking.models[0].coding == 35.0


def test_out_of_bounds_intelligence_raises() -> None:
    """An intelligence index above 100 fails loud with no partial ranking."""
    page = {"pagination": {"total_pages": 1}, "data": [_row("Bad", "bad", 140.0)]}
    with pytest.raises(AaIngestError):
        parse_aa_models([page])


def test_negative_price_raises() -> None:
    """A negative price is a corrupt scrape and fails loud."""
    page = {"pagination": {"total_pages": 1}, "data": [_row("Bad", "bad", 50.0, price_in=-1.0)]}
    with pytest.raises(AaIngestError):
        parse_aa_models([page])


def test_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing API key raises before any fetch is attempted."""
    monkeypatch.delenv("REPOACH_ARTIFICIAL_ANALYSIS_API_KEY", raising=False)
    monkeypatch.delenv("ARTIFICIAL_ANALYSIS_API_KEY", raising=False)
    settings = Settings(_env_file=None)

    def _never(page: int) -> dict:
        raise AssertionError("fetch must not be called without a key")

    with pytest.raises(AaIngestError):
        fetch_aa_ranking(settings, fetch_page=_never)


def test_single_page_when_no_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no pagination block the walk fetches exactly page 1."""
    calls: list[int] = []

    def _fetch(page: int) -> dict:
        calls.append(page)
        return {"data": [_row("Only", "only", 25.0)]}

    ranking = fetch_aa_ranking(_settings(monkeypatch), fetch_page=_fetch)
    assert calls == [1]
    assert tuple(model.name for model in ranking.models) == ("only",)
