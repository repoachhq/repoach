"""Unit tests for SP-PROXY-SSE-SINGLE-PARSE.

Pins two properties of the refactored ``_absorb`` dispatch inside
:func:`repoach.llm_proxy.api._failover.peek_for_content`:

1. ``_parse_event`` is invoked at most once per chunk absorbed —
   never a multiple of the chunk count, as it was before the refactor
   introduced private tuple-based helpers shared off a single parse.
2. ``peek_for_content``'s observable decisions are byte-for-byte
   identical to their pre-refactor values across the fixture shapes
   already covered by the sibling failover suites.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import repoach.llm_proxy.api._failover as failover_module
from repoach.llm_proxy.api._failover import PeekResult, peek_for_content


def _sse(event_type: str, payload: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"


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
_TEXT_CONTENT_START = _sse(
    "content_block_start",
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "ok"}},
)
_TOOL_USE_START = _sse(
    "content_block_start",
    {
        "type": "content_block_start",
        "index": 0,
        "content_block": {
            "type": "tool_use",
            "id": "tool_1",
            "name": "send_whatsapp",
            "input": {},
        },
    },
)
_REAL_TEXT_DELTA = _sse(
    "content_block_delta",
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "Hello there"},
    },
)
_CONTENT_BLOCK_STOP = _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
_TERMINAL_REAL_DELTA = _sse(
    "message_delta",
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"input_tokens": 10, "output_tokens": 5},
    },
)
_TERMINAL_EMPTY_DELTA = _sse(
    "message_delta",
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"input_tokens": 10, "output_tokens": 0},
    },
)
_TERMINAL_ERROR_DELTA = _sse(
    "message_delta",
    {
        "type": "message_delta",
        "delta": {"stop_reason": "error", "stop_sequence": None},
        "usage": {"input_tokens": 10, "output_tokens": 0},
    },
)
_MESSAGE_STOP = _sse("message_stop", {"type": "message_stop"})


async def _aiter(chunks: list[str]) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


def test_absorb_parses_each_chunk_exactly_once(monkeypatch) -> None:
    call_count = 0
    original_parse_event = failover_module._parse_event

    def counting_parse_event(chunk: str) -> tuple[str | None, dict | None]:
        nonlocal call_count
        call_count += 1
        return original_parse_event(chunk)

    monkeypatch.setattr(failover_module, "_parse_event", counting_parse_event)

    chunks = [
        _MESSAGE_START,
        _TEXT_CONTENT_START,
        _REAL_TEXT_DELTA,
        _CONTENT_BLOCK_STOP,
        _TERMINAL_REAL_DELTA,
        _MESSAGE_STOP,
    ]

    async def runner() -> PeekResult:
        return await peek_for_content(_aiter(chunks))

    result = asyncio.run(runner())

    assert result.stream_done is True
    assert call_count == len(chunks)


def test_peek_for_content_decisions_unchanged_after_single_parse_refactor() -> None:
    def run(chunks: list[str]) -> PeekResult:
        async def runner() -> PeekResult:
            return await peek_for_content(_aiter(chunks))

        return asyncio.run(runner())

    tool_use_result = run([_MESSAGE_START, _TOOL_USE_START, _TERMINAL_REAL_DELTA, _MESSAGE_STOP])
    assert tool_use_result.got_content is True
    assert tool_use_result.stream_done is True
    assert tool_use_result.looks_budget_starved is False
    assert tool_use_result.final_output_tokens == 5
    assert tool_use_result.upstream_status_code is None
    assert len(tool_use_result.buffered) == 4

    zero_tokens_result = run([_MESSAGE_START, _TERMINAL_EMPTY_DELTA, _MESSAGE_STOP])
    assert zero_tokens_result.got_content is False
    assert zero_tokens_result.stream_done is True
    assert zero_tokens_result.looks_budget_starved is True
    assert zero_tokens_result.final_output_tokens == 0
    assert zero_tokens_result.upstream_status_code is None
    assert len(zero_tokens_result.buffered) == 3

    real_text_result = run(
        [
            _MESSAGE_START,
            _TEXT_CONTENT_START,
            _REAL_TEXT_DELTA,
            _CONTENT_BLOCK_STOP,
            _TERMINAL_REAL_DELTA,
            _MESSAGE_STOP,
        ]
    )
    assert real_text_result.got_content is True
    assert real_text_result.stream_done is True
    assert real_text_result.looks_budget_starved is False
    assert real_text_result.final_output_tokens == 5
    assert real_text_result.upstream_status_code is None
    assert len(real_text_result.buffered) == 6

    error_stop_result = run([_MESSAGE_START, _TERMINAL_ERROR_DELTA])
    assert error_stop_result.got_content is False
    assert error_stop_result.stream_done is False
    assert error_stop_result.looks_budget_starved is False
    assert error_stop_result.final_output_tokens == 0
    assert error_stop_result.upstream_status_code is None
    assert len(error_stop_result.buffered) == 2
