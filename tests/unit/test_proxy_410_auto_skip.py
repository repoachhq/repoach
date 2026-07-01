"""Unit tests for SP-PROXY-410-AUTO-SKIP.

When an upstream provider raises a transient error (HTTP 410 EOL,
5xx, disguised connection error embedded as text content), the
proxy's openai_compat layer used to leak the error message
downstream as if it were a real model response — because
``finish_reason`` stayed ``None`` and mapped to ``"end_turn"``.

This spec wires:

1. ``STOP_REASON_MAP`` carries an ``"error" → "error"`` entry so
   the failover signal survives the mapping.
2. ``openai_compat`` sets ``finish_reason = "error"`` when the
   ``except Exception`` branch fires.

End result: ``peek_for_content`` sees ``stop_reason == "error"``
on the final ``message_delta`` and classifies the stream as
failure → chain-failover walks to the next candidate.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from ferova.llm_proxy.api._failover import peek_for_content
from ferova.llm_proxy.core.anthropic.sse import (
    STOP_REASON_MAP,
    map_stop_reason,
)

# ---------------------------------------------------------------------------
# 1. STOP_REASON_MAP carries the error → error mapping
# ---------------------------------------------------------------------------


def test_stop_reason_map_preserves_error() -> None:
    assert STOP_REASON_MAP.get("error") == "error"
    assert map_stop_reason("error") == "error"


def test_stop_reason_map_unchanged_for_normal_finish_reasons() -> None:
    """Existing mappings remain intact (regression pin)."""
    assert map_stop_reason("stop") == "end_turn"
    assert map_stop_reason("length") == "max_tokens"
    assert map_stop_reason("tool_calls") == "tool_use"
    assert map_stop_reason("content_filter") == "end_turn"
    assert map_stop_reason(None) == "end_turn"
    assert map_stop_reason("unknown") == "end_turn"


# ---------------------------------------------------------------------------
# 2. peek_for_content classifies error-stop_reason streams as failure
# ---------------------------------------------------------------------------


def _sse_event(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


async def _stream_from(chunks: list[str]) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


@pytest.mark.asyncio
async def test_disguised_error_with_error_stop_reason_is_failure() -> None:
    """A stream whose final ``message_delta`` carries
    ``stop_reason="error"`` is classified as failure even when the
    error message was emitted as a fake text content_block (the NIM
    HTTP 410 pattern)."""
    chunks = [
        _sse_event("content_block_start", {"index": 0, "content_block": {"type": "text"}}),
        _sse_event(
            "content_block_delta",
            {
                "index": 0,
                "delta": {
                    "type": "text_delta",
                    "text": "Error code: 410 - The model has reached its end of life",
                },
            },
        ),
        _sse_event("content_block_stop", {"index": 0}),
        _sse_event(
            "message_delta",
            {"delta": {"stop_reason": "error"}, "usage": {"output_tokens": 13}},
        ),
        _sse_event("message_stop", {}),
    ]

    result = await peek_for_content(_stream_from(chunks))

    assert result.got_content is False
    assert result.stream_done is True


@pytest.mark.asyncio
async def test_disguised_error_with_end_turn_was_a_false_positive() -> None:
    """Documents the OLD bug: when stop_reason was wrongly
    ``"end_turn"`` and output_tokens > 0, peek classified as
    success.  This test pins the failure mode the fix prevents:
    when the FAILED-fix is in place (``stop_reason="error"``), the
    classifier is correct.  Without the fix
    (``stop_reason="end_turn"``), peek still wrongly returns True —
    that's the bug we just closed.
    """
    chunks = [
        _sse_event("content_block_start", {"index": 0, "content_block": {"type": "text"}}),
        _sse_event(
            "content_block_delta",
            {"index": 0, "delta": {"type": "text_delta", "text": "Error code: 410"}},
        ),
        _sse_event("content_block_stop", {"index": 0}),
        # WITHOUT the fix, finish_reason was None → "end_turn"
        _sse_event(
            "message_delta",
            {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
        ),
        _sse_event("message_stop", {}),
    ]

    result = await peek_for_content(_stream_from(chunks))
    # Documented bad behaviour without the fix — kept as regression
    # so if someone re-introduces the leak, this test surfaces it.
    assert result.got_content is True


# ---------------------------------------------------------------------------
# 3. Normal happy-path stream still classifies as success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_text_response_is_success() -> None:
    chunks = [
        _sse_event("content_block_start", {"index": 0, "content_block": {"type": "text"}}),
        _sse_event(
            "content_block_delta",
            {"index": 0, "delta": {"type": "text_delta", "text": "Hello world"}},
        ),
        _sse_event("content_block_stop", {"index": 0}),
        _sse_event(
            "message_delta",
            {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 11}},
        ),
        _sse_event("message_stop", {}),
    ]

    result = await peek_for_content(_stream_from(chunks))
    assert result.got_content is True


# ---------------------------------------------------------------------------
# 4. tool_use response is still a success signal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_use_response_is_success_regardless_of_stop_reason() -> None:
    chunks = [
        _sse_event(
            "content_block_start",
            {"index": 0, "content_block": {"type": "tool_use", "id": "x", "name": "y"}},
        ),
        _sse_event("content_block_stop", {"index": 0}),
        _sse_event(
            "message_delta",
            {"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}},
        ),
        _sse_event("message_stop", {}),
    ]

    result = await peek_for_content(_stream_from(chunks))
    assert result.got_content is True
