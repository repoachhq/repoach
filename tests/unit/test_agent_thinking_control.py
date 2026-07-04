"""Tests for the optional ``thinking`` field on ``AgentRequest``.

Part of SP-AGENT-THINKING-CONTROL step 1/4 — the schema alone must
accept an explicit ``ThinkingConfig`` value and default it to ``None``
for the many existing callers that build ``AgentRequest(...)``
without one, with ``model_dump(exclude_none=True)`` round-tripping
the field cleanly so the dispatcher can copy it verbatim onto the
translated ``MessagesRequest``.
"""

from __future__ import annotations

from ferova.llm_proxy.api.models.agent_v1 import AgentRequest, Message, TextBlock
from ferova.llm_proxy.api.models.anthropic import ThinkingConfig


def _build_request(*, thinking: ThinkingConfig | None = None) -> AgentRequest:
    return AgentRequest(
        schema_version="1",
        capability="sonnet",
        system="You are Ferova's WhatsApp assistant.",
        messages=[Message(role="user", content=[TextBlock(type="text", text="ping")])],
        tools=[],
        thinking=thinking,
    )


def test_agent_request_accepts_thinking_field() -> None:
    """An enabled thinking config round-trips through ``model_dump``.

    The dispatcher copies the field verbatim onto the built
    ``MessagesRequest``; the dump must therefore carry the same
    ``type`` and ``budget_tokens`` the caller supplied.
    """
    thinking = ThinkingConfig(type="enabled", budget_tokens=1024)
    request = _build_request(thinking=thinking)

    assert request.thinking is not None
    assert request.thinking.type == "enabled"
    assert request.thinking.budget_tokens == 1024

    dumped = request.model_dump(exclude_none=True)
    assert "thinking" in dumped
    assert dumped["thinking"]["type"] == "enabled"
    assert dumped["thinking"]["budget_tokens"] == 1024


def test_agent_request_thinking_defaults_to_none() -> None:
    """Existing callers building ``AgentRequest`` without the field keep working.

    ``model_dump(exclude_none=True)`` must omit the field entirely so
    the dispatcher sees no thinking config and falls back to today's
    behaviour (provider global default).
    """
    request = _build_request()

    assert request.thinking is None

    dumped = request.model_dump(exclude_none=True)
    assert "thinking" not in dumped


def test_agent_request_disabled_thinking_round_trips() -> None:
    """A ``disabled`` thinking config survives the dump intact.

    Providers that support an off-switch disable reasoning; others
    strip reasoning output client-side. Either way the dispatcher
    must hand the value through unchanged.
    """
    thinking = ThinkingConfig(type="disabled")
    request = _build_request(thinking=thinking)

    assert request.thinking is not None
    assert request.thinking.type == "disabled"

    dumped = request.model_dump(exclude_none=True)
    assert dumped["thinking"]["type"] == "disabled"


def test_thinking_field_reaches_the_translated_request() -> None:
    """An enabled thinking config on ``AgentRequest`` produces a
    ``MessagesRequest`` carrying the identical config.

    AC1 from SP-AGENT-THINKING-CONTROL.
    """
    from ferova.llm_proxy.api.agent_dispatcher import _translate_request

    thinking = ThinkingConfig(type="enabled", budget_tokens=1024)
    request = _build_request(thinking=thinking)
    translated = _translate_request(request, "test-model")

    assert translated.thinking is not None
    assert translated.thinking.type == "enabled"
    assert translated.thinking.budget_tokens == 1024


def test_absent_thinking_field_translates_to_none() -> None:
    """No field on ``AgentRequest`` → the translated request's
    ``thinking`` is ``None`` (today's behaviour pinned).

    AC2 from SP-AGENT-THINKING-CONTROL.
    """
    from ferova.llm_proxy.api.agent_dispatcher import _translate_request

    request = _build_request()
    translated = _translate_request(request, "test-model")

    assert translated.thinking is None


def test_disabled_thinking_round_trips() -> None:
    """``{"type": "disabled"}`` survives translation intact.

    AC3 from SP-AGENT-THINKING-CONTROL.
    """
    from ferova.llm_proxy.api.agent_dispatcher import _translate_request

    thinking = ThinkingConfig(type="disabled")
    request = _build_request(thinking=thinking)
    translated = _translate_request(request, "test-model")

    assert translated.thinking is not None
    assert translated.thinking.type == "disabled"
