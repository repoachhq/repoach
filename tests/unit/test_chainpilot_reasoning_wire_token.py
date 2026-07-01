"""Wiring tests for the token-budget transports (SP-CHAINPILOT-REASONING-WIRE-TOKEN, 0b-2).

NIM and OpenRouter now route their reasoning bound through the shared
`bounded_reasoning_budget`. These pin the request-body effect:

- NIM still bounds `chat_template_kwargs.reasoning_budget` (byte-identical math).
- OpenRouter applies a DEFAULT `reasoning.max_tokens` when the client set none,
  and still honours an explicit client budget.
"""

from __future__ import annotations

from ferova.llm_proxy.api.models.anthropic import Message, MessagesRequest, ThinkingConfig
from ferova.llm_proxy.config.nim import NimSettings
from ferova.llm_proxy.providers import nvidia_nim
from ferova.llm_proxy.providers.nvidia_nim.request import build_request_body as nim_build
from ferova.llm_proxy.providers.open_router.request import (
    build_request_body as openrouter_build,
)
from ferova.llm_proxy.providers.reasoning import bounded_reasoning_budget


def _request(max_tokens: int, thinking: ThinkingConfig | None = None) -> MessagesRequest:
    return MessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=[Message(role="user", content="ping")],
        thinking=thinking,
    )


class TestNimWiring:
    def test_body_bounds_reasoning_budget(self) -> None:
        body = nim_build(_request(8000), NimSettings(), thinking_enabled=True)
        budget = body["extra_body"]["chat_template_kwargs"]["reasoning_budget"]
        assert budget == bounded_reasoning_budget(body["max_tokens"])
        assert 0 < budget <= 2048

    def test_private_helper_is_gone(self) -> None:
        assert not hasattr(nvidia_nim.request, "_bounded_reasoning_budget")


class TestOpenRouterWiring:
    def test_default_reasoning_budget_applied(self) -> None:
        body = openrouter_build(_request(4000), thinking_enabled=True)
        assert body["reasoning"]["max_tokens"] == bounded_reasoning_budget(body["max_tokens"])

    def test_client_budget_still_wins(self) -> None:
        thinking = ThinkingConfig(type="enabled", budget_tokens=500)
        body = openrouter_build(_request(4000, thinking=thinking), thinking_enabled=True)
        assert body["reasoning"]["max_tokens"] == 500
