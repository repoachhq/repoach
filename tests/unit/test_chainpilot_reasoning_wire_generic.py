"""Wiring tests for the generic transport (SP-CHAINPILOT-REASONING-WIRE-GENERIC, 0b-3).

The generic OpenAI provider applies the reasoning plan: the max_tokens headroom
floor, the thinking-disable toggle (kimi/deepseek), and — since 2a-3-iv-b
(SP-CHAINPILOT-EFFORT-APPLY) — the per-model reasoning_effort knob, but only for
the cells the runtime effort map has resolved. These tests pin the conservative
default (no effort emitted when the map is unseeded) and the positive wiring
(effort emitted for a seeded cell).
"""

from __future__ import annotations

import json

import pytest

from repoach.llm_proxy.api.models.anthropic import Message, MessagesRequest, ThinkingConfig
from repoach.llm_proxy.providers.base import ProviderConfig
from repoach.llm_proxy.providers.effort_map import get_effort_map, reset_effort_map
from repoach.llm_proxy.providers.openai_generic import GenericOpenAIProvider


@pytest.fixture(autouse=True)
def _isolate_effort_map():
    reset_effort_map()
    yield
    reset_effort_map()


def _provider(provider_name: str) -> GenericOpenAIProvider:
    config = ProviderConfig(api_key="test-key", base_url="https://upstream.example/v1")
    return GenericOpenAIProvider(config, provider_name=provider_name)


def _request(max_tokens: int, thinking: ThinkingConfig | None = None) -> MessagesRequest:
    return MessagesRequest(
        model="some-model",
        max_tokens=max_tokens,
        messages=[Message(role="user", content="ping")],
        thinking=thinking,
    )


class TestHeadroomFloor:
    def test_thinking_on_raises_max_tokens_to_floor(self) -> None:
        body = _provider("kimi")._build_request_body(_request(1000))
        assert body["max_tokens"] == 4096

    def test_max_tokens_already_above_floor_is_kept(self) -> None:
        body = _provider("groq")._build_request_body(_request(8000))
        assert body["max_tokens"] == 8000


class TestToggleDisable:
    def test_kimi_disables_thinking_when_off(self) -> None:
        body = _provider("kimi")._build_request_body(
            _request(2000, thinking=ThinkingConfig(type="disabled", enabled=False))
        )
        assert body["extra_body"]["thinking"] == {"type": "disabled"}

    def test_groq_does_not_get_a_toggle(self) -> None:
        body = _provider("groq")._build_request_body(
            _request(2000, thinking=ThinkingConfig(type="disabled", enabled=False))
        )
        assert "thinking" not in body.get("extra_body", {})


class TestEffortApplication:
    def test_no_reasoning_effort_when_map_unseeded(self) -> None:
        for provider_name in ("kimi", "groq", "cerebras", "deepseek"):
            body = _provider(provider_name)._build_request_body(_request(8000))
            assert "reasoning_effort" not in json.dumps(body)

    def test_unknown_provider_adds_no_reasoning_fields(self) -> None:
        body = _provider("mystery")._build_request_body(_request(1000))
        assert body["max_tokens"] == 1000
        assert "extra_body" not in body

    def test_seeded_cell_emits_resolved_effort(self) -> None:
        get_effort_map().replace({("groq", "some-model"): "low"})
        body = _provider("groq")._build_request_body(_request(8000))
        assert body["extra_body"]["reasoning_effort"] == "low"

    def test_unseeded_cell_on_same_provider_emits_nothing(self) -> None:
        get_effort_map().replace({("groq", "other-model"): "low"})
        body = _provider("groq")._build_request_body(_request(8000))
        assert "reasoning_effort" not in json.dumps(body)

    def test_thinking_off_emits_no_effort_but_keeps_toggle(self) -> None:
        get_effort_map().replace({("deepseek", "some-model"): "high"})
        body = _provider("deepseek")._build_request_body(
            _request(2000, thinking=ThinkingConfig(type="disabled", enabled=False))
        )
        assert "reasoning_effort" not in json.dumps(body)
        assert body["extra_body"]["thinking"] == {"type": "disabled"}
