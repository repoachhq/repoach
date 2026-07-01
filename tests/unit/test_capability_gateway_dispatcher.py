"""Tests for the capability gateway dispatcher (SP-CAPABILITY-GATEWAY).

Covers the round-trip ``AgentRequest`` → ``MessagesRequest`` →
SSE-stream aggregation → ``AgentResponse`` for the four core paths
exercised by clients (text-only end_turn, single tool_call, parallel
tool_calls, and the request-side translation of a multi-turn
conversation with ``tool_result`` blocks).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest
from fastapi.responses import StreamingResponse

from ferova.llm_proxy.api.agent_dispatcher import (
    _aggregate_sse_stream,
    _serialise_tool_result,
    _translate_message,
    _translate_request,
    _translate_tool,
    dispatch_agent_request,
)
from ferova.llm_proxy.api.models.agent_v1 import (
    AgentRequest,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    ToolResultError,
    ToolResultOk,
    ToolSpec,
)
from ferova.llm_proxy.api.models.agent_v1 import (
    Message as AgentMessage,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_request(
    *,
    capability: str = "sonnet",
    system: str | None = "You are Ferova's WhatsApp assistant.",
    messages: list[AgentMessage] | None = None,
    tools: list[ToolSpec] | None = None,
) -> AgentRequest:
    return AgentRequest(
        schema_version="1",
        capability=capability,
        system=system,
        messages=messages
        or [AgentMessage(role="user", content=[TextBlock(type="text", text="ping")])],
        tools=tools or [],
    )


def _sse(events: list[dict]) -> str:
    """Render a sequence of dicts as Anthropic-style SSE chunks."""
    parts: list[str] = []
    for event in events:
        parts.append(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n")
    return "".join(parts)


async def _async_chunks(*chunks: str) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk.encode("utf-8")


# ---------------------------------------------------------------------------
# Request translation
# ---------------------------------------------------------------------------


def test_translate_request_maps_capability_to_alias() -> None:
    req = _build_request(capability="sonnet")
    out = _translate_request(req, "claude-sonnet-4-6")
    assert out.model == "claude-sonnet-4-6"
    assert out.system == "You are Ferova's WhatsApp assistant."
    assert out.stream is True


def test_translate_message_text_only() -> None:
    msg = AgentMessage(role="user", content=[TextBlock(type="text", text="hello")])
    out = _translate_message(msg)
    assert out.role == "user"
    assert len(out.content) == 1
    block = out.content[0]
    assert block.type == "text"
    assert block.text == "hello"


def test_translate_message_tool_call_uses_anthropic_tool_use_shape() -> None:
    msg = AgentMessage(
        role="assistant",
        content=[
            ToolCallBlock(
                type="tool_call",
                id="call_abc",
                name="query_l4",
                args={"sql": "SELECT 1"},
            )
        ],
    )
    out = _translate_message(msg)
    block = out.content[0]
    assert block.type == "tool_use"
    assert block.id == "call_abc"
    assert block.name == "query_l4"
    assert block.input == {"sql": "SELECT 1"}


def test_translate_message_tool_result_serialises_typed_union() -> None:
    msg = AgentMessage(
        role="user",
        content=[
            ToolResultBlock(
                type="tool_result",
                tool_call_id="call_abc",
                result=ToolResultOk(ok=True, value={"rows": [1, 2, 3]}),
            )
        ],
    )
    out = _translate_message(msg)
    block = out.content[0]
    assert block.type == "tool_result"
    assert block.tool_use_id == "call_abc"
    payload = json.loads(block.content)
    assert payload == {"ok": True, "value": {"rows": [1, 2, 3]}}


def test_message_role_rejects_system_at_validation_time() -> None:
    """``system`` lives at top-level only — pydantic refuses it in messages[]."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AgentMessage(role="system", content=[TextBlock(type="text", text="be nice")])


def test_translate_tool_renames_parameters_schema_to_input_schema() -> None:
    spec = ToolSpec(
        name="echo",
        description="echo back",
        parameters_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    )
    out = _translate_tool(spec)
    assert out.name == "echo"
    assert out.description == "echo back"
    assert out.input_schema == {"type": "object", "properties": {"text": {"type": "string"}}}


def test_serialise_tool_result_error_branch() -> None:
    err = ToolResultError(ok=False, error={"code": "sql_error", "message": "no such table"})
    payload = json.loads(_serialise_tool_result(err))
    assert payload == {"ok": False, "error": {"code": "sql_error", "message": "no such table"}}


# ---------------------------------------------------------------------------
# SSE aggregation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aggregate_sse_collects_text_block() -> None:
    stream = _async_chunks(
        _sse(
            [
                {
                    "type": "message_start",
                    "message": {
                        "model": "nvidia_nim/meta/llama-3.3-70b-instruct",
                        "usage": {"input_tokens": 100, "output_tokens": 0},
                    },
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Hello"},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": " world"},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 12},
                },
                {"type": "message_stop"},
            ]
        )
    )
    aggregated = await _aggregate_sse_stream(stream)
    assert aggregated["model"] == "nvidia_nim/meta/llama-3.3-70b-instruct"
    assert aggregated["stop_reason"] == "end_turn"
    assert len(aggregated["content_blocks"]) == 1
    assert aggregated["content_blocks"][0]["type"] == "text"
    assert aggregated["content_blocks"][0]["text"] == "Hello world"
    assert aggregated["usage"].input_tokens == 100
    assert aggregated["usage"].output_tokens == 12
    assert aggregated["usage"].total_tokens == 112


@pytest.mark.asyncio
async def test_aggregate_sse_collects_tool_use_with_chunked_input_json() -> None:
    stream = _async_chunks(
        _sse(
            [
                {
                    "type": "message_start",
                    "message": {
                        "model": "groq/llama-3.3-70b-versatile",
                        "usage": {"input_tokens": 50, "output_tokens": 0},
                    },
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "query_l4",
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"sql":'},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '"SELECT 1"}'},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 30},
                },
                {"type": "message_stop"},
            ]
        )
    )
    aggregated = await _aggregate_sse_stream(stream)
    assert aggregated["stop_reason"] == "tool_use"
    block = aggregated["content_blocks"][0]
    assert block["type"] == "tool_use"
    assert block["id"] == "call_1"
    assert block["name"] == "query_l4"
    assert block["input_json"] == '{"sql":"SELECT 1"}'


@pytest.mark.asyncio
async def test_aggregate_sse_skips_done_marker() -> None:
    stream = _async_chunks(
        "event: message_stop\ndata: [DONE]\n\n",
    )
    aggregated = await _aggregate_sse_stream(stream)
    # No exceptions; empty result is fine.
    assert aggregated["content_blocks"] == []
    assert aggregated["stop_reason"] == ""


# ---------------------------------------------------------------------------
# End-to-end dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_agent_request_returns_text_response() -> None:
    request = _build_request()
    fake_stream = _async_chunks(
        _sse(
            [
                {
                    "type": "message_start",
                    "message": {
                        "model": "nvidia_nim/meta/llama-3.3-70b-instruct",
                        "usage": {"input_tokens": 42, "output_tokens": 0},
                    },
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "ok"},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 5},
                },
                {"type": "message_stop"},
            ]
        )
    )
    streaming_response = StreamingResponse(fake_stream, media_type="text/event-stream")
    service = MagicMock()
    service.create_message = MagicMock(return_value=streaming_response)

    response = await dispatch_agent_request(request, service)

    assert response.schema_version == "1"
    assert response.capability == "sonnet"
    assert response.stop_reason == "end_turn"
    assert response.model_used == "nvidia_nim/meta/llama-3.3-70b-instruct"
    assert len(response.content) == 1
    assert response.content[0].type == "text"
    assert response.content[0].text == "ok"
    assert response.usage.input_tokens == 42
    assert response.usage.output_tokens == 5
    assert response.usage.total_tokens == 47
    assert response.elapsed_ms >= 0
    assert len(response.trace) == 1
    assert response.trace[0].outcome == "ok"
    assert response.error is None


@pytest.mark.asyncio
async def test_dispatch_agent_request_with_tool_call_in_history() -> None:
    """A multi-turn request with a prior tool_call + tool_result still translates cleanly."""
    request = AgentRequest(
        schema_version="1",
        capability="sonnet",
        system="be helpful",
        messages=[
            AgentMessage(role="user", content=[TextBlock(type="text", text="what time is it?")]),
            AgentMessage(
                role="assistant",
                content=[ToolCallBlock(type="tool_call", id="call_t", name="get_time", args={})],
            ),
            AgentMessage(
                role="user",
                content=[
                    ToolResultBlock(
                        type="tool_result",
                        tool_call_id="call_t",
                        result=ToolResultOk(ok=True, value={"hour": 9, "minute": 30}),
                    )
                ],
            ),
        ],
    )
    fake_stream = _async_chunks(
        _sse(
            [
                {
                    "type": "message_start",
                    "message": {
                        "model": "nvidia_nim/meta/llama-3.3-70b-instruct",
                        "usage": {"input_tokens": 88, "output_tokens": 0},
                    },
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "9h30."},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 4},
                },
                {"type": "message_stop"},
            ]
        )
    )
    streaming_response = StreamingResponse(fake_stream, media_type="text/event-stream")
    service = MagicMock()
    service.create_message = MagicMock(return_value=streaming_response)

    response = await dispatch_agent_request(request, service)

    assert response.stop_reason == "end_turn"
    assert response.content[0].text == "9h30."
    # Service received an Anthropic-shaped MessagesRequest with all 3 turns.
    sent_request = service.create_message.call_args.args[0]
    assert len(sent_request.messages) == 3
    assert sent_request.messages[1].content[0].type == "tool_use"
    assert sent_request.messages[2].content[0].type == "tool_result"
