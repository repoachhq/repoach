"""Tests for tier classification (SP-ROUTING-DOMAIN-TYPES).

Locks :func:`classify_tier` to each branch of the legacy
``Settings.resolve_models`` substring logic (``settings.py:407-420``),
including first-match-wins order and the H10 explicit-provider
passthrough.
"""

from __future__ import annotations

import pytest

from repoach.llm_proxy.routing.tier import Tier, classify_tier


@pytest.mark.parametrize(
    ("name", "tier"),
    [
        ("claude-coder-ferova", Tier.DEFAULT),
        ("claude-opus-4-7", Tier.OPUS),
        ("claude-3-5-haiku", Tier.HAIKU),
        ("claude-sonnet-4", Tier.SONNET),
        ("gpt-4o", Tier.DEFAULT),
        ("nvidia_nim/mistralai/x", Tier.DEFAULT),
        ("open_router/anything", Tier.DEFAULT),
        ("claude_code/opus", Tier.DEFAULT),
        ("unknown_provider/opus", Tier.OPUS),
    ],
)
def test_classify_tier(name: str, tier: Tier) -> None:
    assert classify_tier(name) == tier


def test_coder_substring_no_longer_classifies() -> None:
    assert classify_tier("opus-coder-model") == Tier.OPUS
    assert classify_tier("just-coder") == Tier.DEFAULT


def test_haiku_wins_over_sonnet() -> None:
    assert classify_tier("sonnet-haiku-blend") == Tier.HAIKU
