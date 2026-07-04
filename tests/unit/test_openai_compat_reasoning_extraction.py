"""Tests for reading ``completion_tokens_details.reasoning_tokens`` in the
OpenAI-compatible transport (SP-USAGE-REASONING-SPLIT step 2/4).

Covers the ``_extract_reasoning_tokens`` helper and the fallback to
``SSEBuilder.estimate_reasoning_tokens()`` when upstream usage is absent.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from ferova.llm_proxy.providers.base import ProviderConfig
from ferova.llm_proxy.providers.openai_compat import (
    OpenAIChatTransport,
    _extract_reasoning_tokens,
)


class _FakeCompletionTokensDetails:
    """Mimics ``openai.types.CompletionTokensDetails``."""

    def __init__(self, reasoning_tokens: int | None = None) -> None:
        self.reasoning_tokens = reasoning_tokens


class _FakeUsage:
    """Mimics ``openai.types.CompletionUsage``."""

    def __init__(
        self,
        completion_tokens: int = 100,
        prompt_tokens: int = 50,
        completion_tokens_details: _FakeCompletionTokensDetails | None = None,
    ) -> None:
        self.completion_tokens = completion_tokens
        self.prompt_tokens = prompt_tokens
        self.completion_tokens_details = completion_tokens_details


class _FakeDelta:
    """Mimics an OpenAI chat delta with only the fields the loop touches."""

    def __init__(self) -> None:
        self.content: str | None = "Hello"
        self.reasoning_content: str | None = None
        self.tool_calls: list | None = None


class _FakeChoice:
    """Mimics an OpenAI chat choice."""

    def __init__(
        self,
        delta: _FakeDelta | None = None,
        finish_reason: str | None = "stop",
    ) -> None:
        self.delta = delta or _FakeDelta()
        self.finish_reason = finish_reason


class _FakeChunk:
    """Mimics an OpenAI chat completion chunk."""

    def __init__(
        self,
        choices: list[_FakeChoice] | None = None,
        usage: _FakeUsage | None = None,
    ) -> None:
        self.choices = choices or [_FakeChoice()]
        self.usage = usage


class _TransportForTest(OpenAIChatTransport):
    """Minimal concrete transport returning scripted chunks via ``_create_stream``."""

    def __init__(
        self,
        chunks: list[_FakeChunk],
        *,
        provider_name: str = "test",
    ) -> None:
        config = ProviderConfig(api_key="test-key", base_url="https://test.example/v1")
        super().__init__(
            config,
            provider_name=provider_name,
            base_url=config.base_url or "",
            api_key=config.api_key,
        )
        self._chunks = chunks

    def _build_request_body(self, request: Any) -> dict:
        return {"model": "test-model", "messages": []}

    async def _create_stream(self, body: dict) -> tuple[Any, dict]:
        """Return a fake stream that yields the scripted chunks."""

        async def _stream() -> AsyncIterator[_FakeChunk]:
            for chunk in self._chunks:
                yield chunk

        return _stream(), body


class _FakeRequest:
    """Minimal request object with the attributes the transport reads."""

    model: str = "test-model"
    thinking: dict | None = None


def _drain_impl(transport: OpenAIChatTransport) -> list[str]:
    """Run ``_stream_response_impl`` and collect all SSE strings."""

    async def _runner() -> list[str]:
        events: list[str] = []
        async for event in transport._stream_response_impl(
            _FakeRequest(), input_tokens=10, request_id=None
        ):
            events.append(event)
        return events

    import asyncio

    return asyncio.run(_runner())


def _parse_message_delta(events: list[str]) -> dict:
    """Find and parse the last ``message_delta`` event in *events*."""
    for event in reversed(events):
        if "event: message_delta" in event:
            for line in event.splitlines():
                if line.startswith("data: "):
                    return json.loads(line[len("data: ") :])
    raise AssertionError("No message_delta event found")


class TestExtractReasoningTokensUnit:
    """Direct unit tests for ``_extract_reasoning_tokens``."""

    def test_reads_value_from_details(self) -> None:
        details = _FakeCompletionTokensDetails(reasoning_tokens=1200)
        usage = _FakeUsage(completion_tokens=2500, completion_tokens_details=details)
        assert _extract_reasoning_tokens(usage, "test") == 1200

    def test_missing_details_attribute_defaults_to_zero(self) -> None:
        usage = _FakeUsage(completion_tokens=100)
        del usage.completion_tokens_details
        assert _extract_reasoning_tokens(usage, "test") == 0

    def test_none_details_defaults_to_zero(self) -> None:
        usage = _FakeUsage(completion_tokens=100, completion_tokens_details=None)
        assert _extract_reasoning_tokens(usage, "test") == 0

    def test_none_value_in_details_defaults_to_zero(self) -> None:
        details = _FakeCompletionTokensDetails(reasoning_tokens=None)
        usage = _FakeUsage(completion_tokens=100, completion_tokens_details=details)
        assert _extract_reasoning_tokens(usage, "test") == 0

    def test_non_integer_value_defaults_to_zero(self) -> None:
        details = _FakeCompletionTokensDetails()
        details.reasoning_tokens = "twelve-hundred"
        usage = _FakeUsage(completion_tokens=100, completion_tokens_details=details)
        assert _extract_reasoning_tokens(usage, "test") == 0

    def test_none_usage_attribute_returns_zero(self) -> None:
        usage = _FakeUsage(completion_tokens=100)
        object.__setattr__(usage, "completion_tokens_details", None)
        assert _extract_reasoning_tokens(usage, "test") == 0


class TestCompletionTokensDetailsReasoningIsRead:
    """Upstream ``completion_tokens_details.reasoning_tokens`` lands in the SSE."""

    def test_completion_tokens_details_reasoning_is_read(self) -> None:
        details = _FakeCompletionTokensDetails(reasoning_tokens=42)
        usage = _FakeUsage(completion_tokens=100, completion_tokens_details=details)
        chunk = _FakeChunk(
            choices=[_FakeChoice(finish_reason="stop")],
            usage=usage,
        )
        transport = _TransportForTest([chunk])
        events = _drain_impl(transport)
        delta = _parse_message_delta(events)

        assert delta["reasoning_tokens"] == 42
        assert "reasoning_tokens" not in delta["usage"]

    def test_missing_completion_tokens_details_defaults_to_zero(self) -> None:
        usage = _FakeUsage(completion_tokens=100)
        del usage.completion_tokens_details
        chunk = _FakeChunk(
            choices=[_FakeChoice(finish_reason="stop")],
            usage=usage,
        )
        transport = _TransportForTest([chunk])
        events = _drain_impl(transport)
        delta = _parse_message_delta(events)

        assert delta["reasoning_tokens"] == 0

    def test_non_integer_detail_value_defaults_to_zero_and_logs(self) -> None:
        from loguru import logger as _loguru

        details = _FakeCompletionTokensDetails()
        details.reasoning_tokens = "bogus"
        usage = _FakeUsage(completion_tokens=100, completion_tokens_details=details)
        chunk = _FakeChunk(
            choices=[_FakeChoice(finish_reason="stop")],
            usage=usage,
        )
        transport = _TransportForTest([chunk])

        captured: list[str] = []

        def _sink(message: Any) -> None:
            record = message.record if hasattr(message, "record") else message
            captured.append(str(record["message"]))

        handler_id = _loguru.add(_sink, level="DEBUG")
        try:
            events = _drain_impl(transport)
        finally:
            _loguru.remove(handler_id)

        delta = _parse_message_delta(events)

        assert delta["reasoning_tokens"] == 0
        assert any("reasoning_tokens non-integer" in m for m in captured)

    def test_absent_usage_falls_back_to_sse_estimate(self) -> None:
        chunk = _FakeChunk(
            choices=[_FakeChoice(finish_reason="stop")],
            usage=None,
        )
        transport = _TransportForTest([chunk])
        events = _drain_impl(transport)
        delta = _parse_message_delta(events)

        assert delta["reasoning_tokens"] == 0
