"""Tests for SP-CHAINPILOT-EQUIVALENCES (Phase 1d).

Covers the pure resolver with an inline fixture, separator/case-insensitive
matching, fail-loud validation, and an honesty cross-check that every alias in
the shipped mapping is a real name in the 1c benchmark snapshot.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from repoach.llm_proxy.providers.benchmark_equivalences import (
    EquivalenceTable,
    load_equivalence_table,
    parse_equivalence_table,
)
from repoach.llm_proxy.providers.benchmark_prior import load_benchmark_ranking

_FIXTURE = {
    "equivalences": [
        {
            "canonical": "deepseek-v4-pro",
            "aliases": ["DeepSeek V4 Pro (Max)"],
            "id_patterns": ["deepseek-v4-pro"],
        },
        {
            "canonical": "qwen3.7-max",
            "aliases": ["Qwen3.7 Max", "Qwen3.7-Max"],
            "id_patterns": ["qwen3.7-max"],
        },
    ]
}


def test_aliases_for_model_id_matches_by_pattern() -> None:
    table = parse_equivalence_table(_FIXTURE)

    assert table.aliases_for_model_id("deepseek-ai/deepseek-v4-pro") == ("DeepSeek V4 Pro (Max)",)
    assert table.aliases_for_model_id("nvidia_nim/mistralai/mistral-medium-3.5") == ()


def test_canonical_for_model_id() -> None:
    table = parse_equivalence_table(_FIXTURE)

    assert table.canonical_for_model_id("qwen/qwen3.7-max") == "qwen3.7-max"
    assert table.canonical_for_model_id("unmatched/model") is None


def test_alias_lookups() -> None:
    table = parse_equivalence_table(_FIXTURE)

    assert table.canonical_for_alias("Qwen3.7-Max") == "qwen3.7-max"
    assert table.id_patterns_for_alias("DeepSeek V4 Pro (Max)") == ("deepseek-v4-pro",)
    assert table.canonical_for_alias("Unknown Model") is None
    assert table.id_patterns_for_alias("Unknown Model") == ()


def test_matching_is_separator_and_case_insensitive() -> None:
    table = parse_equivalence_table(_FIXTURE)

    for variant in ("DeepSeek_V4_PRO", "deepseek/deepseek.v4.pro", "DEEPSEEK-V4-PRO"):
        assert table.canonical_for_model_id(variant) == "deepseek-v4-pro"


def test_malformed_payload_fails_loud() -> None:
    with pytest.raises(ValidationError):
        parse_equivalence_table({"equivalences": [{"canonical": "x", "aliases": ["a"]}]})


def test_shipped_aliases_are_real_snapshot_names() -> None:
    table = load_equivalence_table()
    snapshot_names = set(load_benchmark_ranking().model_names())

    assert isinstance(table, EquivalenceTable)
    assert len(table.equivalences) > 0
    for equivalence in table.equivalences:
        assert equivalence.aliases
        for alias in equivalence.aliases:
            assert alias in snapshot_names, f"alias not in 1c snapshot: {alias!r}"
