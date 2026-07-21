"""Unit tests for SP-BREAKER-SLOW-STRIKE — PeekResult carries output tokens.

These tests verify that :class:`PeekResult` exposes the
``usage.output_tokens`` from the final ``message_delta`` emitted by an
SSE stream, so the slow-completion policy in the services layer can
compute tokens-per-second without re-parsing the buffered events.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from repoach.llm_proxy.api._failover import PeekResult, peek_for_content


def _sse(event_type: str, payload: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"


async def _aiter(chunks: list[str]) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


_MESSAGE_START = _sse(
    "message_start",
    {
        "type": "message_start",
        "message": {
            "id": "msg_x",
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": "fake",
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 0},
        },
    },
)

_MESSAGE_DELTA_SEVEN_TOKENS = _sse(
    "message_delta",
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"input_tokens": 10, "output_tokens": 7},
    },
)

_MESSAGE_STOP = _sse("message_stop", {"type": "message_stop"})


def test_peek_result_carries_output_tokens() -> None:
    """A stream carrying a message_delta with output_tokens: 7 must produce
    a PeekResult whose final_output_tokens is 7."""

    async def runner() -> PeekResult:
        return await peek_for_content(
            _aiter([_MESSAGE_START, _MESSAGE_DELTA_SEVEN_TOKENS, _MESSAGE_STOP])
        )

    result = asyncio.run(runner())
    assert result.final_output_tokens == 7


def test_peek_result_output_tokens_none_on_absent_delta() -> None:
    """A stream without a message_delta must produce a PeekResult whose
    final_output_tokens is None."""

    async def runner() -> PeekResult:
        return await peek_for_content(_aiter([_MESSAGE_START, _MESSAGE_STOP]))

    result = asyncio.run(runner())
    assert result.final_output_tokens is None
