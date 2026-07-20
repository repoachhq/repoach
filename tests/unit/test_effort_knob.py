"""Tests for the reasoning-effort wire fragment (SP-CHAINPILOT-EFFORT-KNOB)."""

from __future__ import annotations

import pytest

from repoach.llm_proxy.providers.effort_knob import (
    REASONING_EFFORT_FIELD,
    effort_extra_body,
    probe_effort_for,
)


@pytest.mark.parametrize(
    ("provider_id", "effort", "expected"),
    [
        ("groq", "low", {"reasoning_effort": "low"}),
        ("cerebras", "low", {"reasoning_effort": "low"}),
        ("deepseek", "high", {"reasoning_effort": "high"}),
    ],
)
def test_effort_extra_body_for_effort_providers(provider_id, effort, expected):
    assert effort_extra_body(provider_id, effort) == expected


@pytest.mark.parametrize("provider_id", ["nvidia_nim", "open_router", "kimi", "unknown"])
def test_effort_extra_body_empty_for_non_effort_providers(provider_id):
    assert effort_extra_body(provider_id, "low") == {}


@pytest.mark.parametrize("effort", [None, ""])
def test_effort_extra_body_empty_when_no_value(effort):
    assert effort_extra_body("groq", effort) == {}


def test_effort_extra_body_returns_fresh_mergeable_dict():
    first = effort_extra_body("groq", "low")
    first["reasoning_effort"] = "mutated"
    second = effort_extra_body("groq", "low")
    assert second == {"reasoning_effort": "low"}

    merged = {"max_tokens": 1, **effort_extra_body("groq", "low")}
    assert merged == {"max_tokens": 1, "reasoning_effort": "low"}


def test_reasoning_effort_field_name():
    assert REASONING_EFFORT_FIELD == "reasoning_effort"


@pytest.mark.parametrize(
    ("provider_id", "expected"),
    [
        ("groq", "low"),
        ("cerebras", "low"),
        ("deepseek", "high"),
        ("nvidia_nim", None),
        ("open_router", None),
        ("kimi", None),
        ("unknown", None),
    ],
)
def test_probe_effort_for(provider_id, expected):
    assert probe_effort_for(provider_id) == expected


@pytest.mark.parametrize(
    "provider_id",
    ["groq", "cerebras", "deepseek", "nvidia_nim", "open_router", "kimi", "unknown"],
)
def test_probe_effort_round_trips_into_fragment(provider_id):
    fragment = effort_extra_body(provider_id, probe_effort_for(provider_id))
    if probe_effort_for(provider_id) is None:
        assert fragment == {}
    else:
        assert fragment == {"reasoning_effort": probe_effort_for(provider_id)}
