"""Acceptance tests for SP-USAGE-REASONING-SPLIT step 4/4.

Covers the agent_v1 ``Usage.reasoning_tokens`` field as populated by
``_aggregate_sse_stream`` in the agent dispatcher, and confirms the
``/v1/messages`` usage object stays byte-compatible (no new key), per
the spec's AC1-AC4.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from ferova.llm_proxy.api.agent_dispatcher import _aggregate_sse_stream
from ferova.llm_proxy.core.anthropic.sse import SSEBuilder


def _sse(events: list[dict]) -> str:
    """Render a sequence of dicts as Anthropic-style SSE chunks."""
    parts: list[str] = []
    for event in events:
        parts.append(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n")
    return "".join(parts)


async def _async_chunks(*chunks: str) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk.encode("utf-8")


@pytest.mark.asyncio
async def test_upstream_reasoning_detail_is_surfaced() -> None:
    """AC1: a message_delta carrying a reasoning_tokens sibling surfaces on Usage.

    Mirrors the sibling field Step 2's openai_chat transport emits from an
    upstream ``completion_tokens_details.reasoning_tokens`` value.
    """
    stream = _async_chunks(
        _sse(
            [
                {
                    "type": "message_start",
                    "message": {
                        "model": "nvidia_nim/deepseek-ai/deepseek-r1",
                        "usage": {"input_tokens": 100, "output_tokens": 0},
                    },
                },
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 2500},
                    "reasoning_tokens": 1200,
                },
                {"type": "message_stop"},
            ]
        )
    )
    aggregated = await _aggregate_sse_stream(stream)
    usage = aggregated["usage"]
    assert usage.reasoning_tokens == 1200
    assert usage.output_tokens == 2500


@pytest.mark.asyncio
async def test_missing_detail_defaults_to_zero() -> None:
    """AC2: no reasoning_tokens sibling key means Usage.reasoning_tokens == 0."""
    stream = _async_chunks(
        _sse(
            [
                {
                    "type": "message_start",
                    "message": {
                        "model": "nvidia_nim/meta/llama-3.3-70b-instruct",
                        "usage": {"input_tokens": 40, "output_tokens": 0},
                    },
                },
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 10},
                },
                {"type": "message_stop"},
            ]
        )
    )
    aggregated = await _aggregate_sse_stream(stream)
    usage = aggregated["usage"]
    assert usage.reasoning_tokens == 0
    assert usage.output_tokens == 10


@pytest.mark.asyncio
async def test_estimator_split_survives_when_usage_absent() -> None:
    """AC3: with no upstream usage, the SSE estimator's reasoning share lands
    in ``Usage.reasoning_tokens``.

    The openai_chat transport already emits the estimator's reasoning share
    as the ``reasoning_tokens`` sibling on ``message_delta`` when usage is
    absent (SSEBuilder.estimate_reasoning_tokens — Step 2); this test proves
    the dispatcher's aggregation carries that estimate through unchanged.
    """
    stream = _async_chunks(
        _sse(
            [
                {
                    "type": "message_start",
                    "message": {
                        "model": "nvidia_nim/deepseek-ai/deepseek-r1",
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                },
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 87},
                    "reasoning_tokens": 61,
                },
                {"type": "message_stop"},
            ]
        )
    )
    aggregated = await _aggregate_sse_stream(stream)
    usage = aggregated["usage"]
    assert usage.reasoning_tokens == 61
    assert usage.output_tokens == 87


def test_messages_usage_shape_unchanged() -> None:
    """AC4: the Anthropic-shaped ``/v1/messages`` usage payload never gains
    a ``reasoning_tokens`` key, even though ``message_delta`` carries it as
    a sibling field one level up.
    """
    builder = SSEBuilder(
        message_id="msg_1", model="nvidia_nim/deepseek-ai/deepseek-r1", input_tokens=10
    )
    start_event = builder.message_start()
    delta_event = builder.message_delta("end_turn", output_tokens=50, reasoning_tokens=30)

    for event in (start_event, delta_event):
        for line in event.splitlines():
            if not line.startswith("data:"):
                continue
            payload = json.loads(line[len("data:") :].strip())
            usage = payload.get("usage")
            if usage is None and "message" in payload:
                usage = payload["message"].get("usage")
            assert usage is not None
            assert "reasoning_tokens" not in usage
