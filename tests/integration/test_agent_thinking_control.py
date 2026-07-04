"""End-to-end integration test for SP-AGENT-THINKING-CONTROL.

Drives the real ``dispatch_agent_request`` translator with a fake
service and asserts the ``MessagesRequest`` handed to the dispatch
boundary carries the caller's thinking config verbatim — the exact
hand-off that puts the per-provider thinking machinery in charge —
and that an absent field keeps today's behaviour (``thinking=None``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest
from fastapi.responses import StreamingResponse

from ferova.llm_proxy.api.agent_dispatcher import dispatch_agent_request
from ferova.llm_proxy.api.models.agent_v1 import AgentRequest, TextBlock
from ferova.llm_proxy.api.models.agent_v1 import Message as AgentMessage

_SSE_EVENTS = (
    'event: message_start\ndata: {"type": "message_start", "message": '
    '{"model": "test-model", "usage": {"input_tokens": 5}}}\n\n'
    'event: content_block_start\ndata: {"type": "content_block_start", "index": 0, '
    '"content_block": {"type": "text", "text": ""}}\n\n'
    'event: content_block_delta\ndata: {"type": "content_block_delta", "index": 0, '
    '"delta": {"type": "text_delta", "text": "ok"}}\n\n'
    'event: content_block_stop\ndata: {"type": "content_block_stop", "index": 0}\n\n'
    'event: message_delta\ndata: {"type": "message_delta", '
    '"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}\n\n'
    'event: message_stop\ndata: {"type": "message_stop"}\n\n'
)


async def _sse_stream() -> AsyncIterator[bytes]:
    yield _SSE_EVENTS.encode("utf-8")


def _fake_service() -> MagicMock:
    service = MagicMock()
    service.create_message = MagicMock(
        return_value=StreamingResponse(_sse_stream(), media_type="text/event-stream")
    )
    return service


def _agent_request(thinking: dict | None) -> AgentRequest:
    kwargs = {} if thinking is None else {"thinking": thinking}
    return AgentRequest(
        schema_version="1",
        capability="sonnet",
        system="be helpful",
        messages=[AgentMessage(role="user", content=[TextBlock(type="text", text="hi")])],
        **kwargs,
    )


@pytest.mark.asyncio
async def test_agent_dispatch_forwards_thinking_end_to_end() -> None:
    """The translated MessagesRequest at the service boundary carries thinking."""
    service = _fake_service()
    config = {"type": "enabled", "budget_tokens": 1024}

    response = await dispatch_agent_request(_agent_request(config), service)

    assert response.stop_reason == "end_turn"
    translated = service.create_message.call_args.args[0]
    assert translated.thinking is not None
    assert translated.thinking.type == "enabled"
    assert translated.thinking.budget_tokens == 1024


@pytest.mark.asyncio
async def test_agent_dispatch_without_thinking_stays_none() -> None:
    """An absent thinking field keeps today's behaviour at the boundary."""
    service = _fake_service()

    await dispatch_agent_request(_agent_request(None), service)

    translated = service.create_message.call_args.args[0]
    assert translated.thinking is None
