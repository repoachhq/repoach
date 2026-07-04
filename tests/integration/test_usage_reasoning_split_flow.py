"""End-to-end integration test for SP-USAGE-REASONING-SPLIT step 4/4.

Drives the agent dispatcher with a fake ``openai_chat``-style transport
stream (via the real ``OpenAIChatTransport._stream_response_impl`` SSE
output) whose final usage carries
``completion_tokens_details.reasoning_tokens=1200``, and asserts the
dispatched ``/v1/agent`` response carries ``usage.reasoning_tokens ==
1200`` while ``output_tokens`` is unchanged from today's accounting.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.responses import StreamingResponse

from ferova.llm_proxy.api.agent_dispatcher import dispatch_agent_request
from ferova.llm_proxy.api.models.agent_v1 import AgentRequest, TextBlock
from ferova.llm_proxy.api.models.agent_v1 import Message as AgentMessage
from ferova.llm_proxy.providers.base import ProviderConfig
from ferova.llm_proxy.providers.openai_compat import OpenAIChatTransport


class _FakeCompletionTokensDetails:
    """Mimics ``openai.types.CompletionTokensDetails``."""

    def __init__(self, reasoning_tokens: int | None = None) -> None:
        self.reasoning_tokens = reasoning_tokens


class _FakeUsage:
    """Mimics ``openai.types.CompletionUsage``."""

    def __init__(
        self,
        completion_tokens: int,
        prompt_tokens: int,
        completion_tokens_details: _FakeCompletionTokensDetails | None = None,
    ) -> None:
        self.completion_tokens = completion_tokens
        self.prompt_tokens = prompt_tokens
        self.completion_tokens_details = completion_tokens_details


class _FakeDelta:
    """Mimics an OpenAI chat delta carrying only assistant text."""

    def __init__(self, content: str | None) -> None:
        self.content = content
        self.reasoning_content: str | None = None
        self.tool_calls: list | None = None


class _FakeChoice:
    """Mimics an OpenAI chat choice."""

    def __init__(self, delta: _FakeDelta, finish_reason: str | None = None) -> None:
        self.delta = delta
        self.finish_reason = finish_reason


class _FakeChunk:
    """Mimics an OpenAI chat completion chunk."""

    def __init__(
        self,
        choices: list[_FakeChoice] | None = None,
        usage: _FakeUsage | None = None,
    ) -> None:
        self.choices = choices or []
        self.usage = usage


class _FakeOpenAIChatTransport(OpenAIChatTransport):
    """Minimal concrete transport that yields a scripted openai_chat stream."""

    def __init__(self, chunks: list[_FakeChunk]) -> None:
        config = ProviderConfig(api_key="test-token", base_url="https://test.example/v1")
        super().__init__(
            config,
            provider_name="test",
            base_url=config.base_url or "",
            api_key=config.api_key,
        )
        self._chunks = chunks

    def _build_request_body(self, request: Any) -> dict:
        return {"model": "test-model", "messages": []}

    async def _create_stream(self, body: dict) -> tuple[Any, dict]:
        async def _stream() -> AsyncIterator[_FakeChunk]:
            for chunk in self._chunks:
                yield chunk

        return _stream(), body


class _FakeUpstreamRequest:
    """Minimal request object with the attributes the transport reads."""

    model: str = "test-model"
    thinking: dict | None = None


async def _sse_from_transport(transport: OpenAIChatTransport) -> AsyncIterator[bytes]:
    async for event in transport._stream_response_impl(
        _FakeUpstreamRequest(), input_tokens=10, request_id=None
    ):
        yield event.encode("utf-8")


def _build_agent_request() -> AgentRequest:
    return AgentRequest(
        schema_version="1",
        capability="sonnet",
        system="be helpful",
        messages=[AgentMessage(role="user", content=[TextBlock(type="text", text="hi")])],
    )


@pytest.mark.asyncio
async def test_agent_dispatch_reports_reasoning_tokens_end_to_end() -> None:
    """A fake openai_chat stream with completion_tokens_details.reasoning_tokens=1200
    ends up on the dispatched /v1/agent response's usage.reasoning_tokens, while
    output_tokens keeps its existing (unsplit) value.
    """
    details = _FakeCompletionTokensDetails(reasoning_tokens=1200)
    usage = _FakeUsage(completion_tokens=2500, prompt_tokens=100, completion_tokens_details=details)
    chunks = [
        _FakeChunk(choices=[_FakeChoice(_FakeDelta("Final answer."))]),
        _FakeChunk(
            choices=[_FakeChoice(_FakeDelta(None), finish_reason="stop")],
            usage=usage,
        ),
    ]
    transport = _FakeOpenAIChatTransport(chunks)

    streaming_response = StreamingResponse(
        _sse_from_transport(transport), media_type="text/event-stream"
    )
    service = MagicMock()
    service.create_message = MagicMock(return_value=streaming_response)

    response = await dispatch_agent_request(_build_agent_request(), service)

    assert response.usage.reasoning_tokens == 1200
    assert response.usage.output_tokens == 2501
    assert response.usage.total_tokens == response.usage.input_tokens + 2501
