"""Unit tests for SP-PROXY-EARLY-ABORT-ERROR-FRAME.

``peek_for_content`` used to drain every chunk of a chain candidate's
SSE stream all the way to ``message_stop`` before evaluating whether a
terminal-error signal (``stop_reason == "error"``, or the documented
disguised-connection-error text) had already fired. This module pins
the per-chunk early-exit: the drain now breaks the instant either
signal appears, and the abandoned stream is explicitly closed via
``aclose()`` rather than left open.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from repoach.llm_proxy.api._failover import PeekResult, peek_for_content

_FAKE_ERROR_TEXT = "Connection error. (request_id=req_def232f1cfca)"


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
_TERMINAL_ERROR_DELTA = _sse(
    "message_delta",
    {
        "type": "message_delta",
        "delta": {"stop_reason": "error", "stop_sequence": None},
        "usage": {"input_tokens": 10, "output_tokens": 0},
    },
)
_FAKE_ERROR_TEXT_DELTA = _sse(
    "content_block_delta",
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": _FAKE_ERROR_TEXT},
    },
)


class _AssertNoFurtherReads:
    """Async iterator that fails the test if drained past its scripted chunks.

    Deliberately exposes no ``aclose`` — proving the ``getattr`` guard in
    :func:`peek_for_content` tolerates a hand-rolled test double that only
    implements ``__aiter__``/``__anext__``.
    """

    def __init__(self, chunks: list[str]) -> None:
        self._remaining = list(chunks)

    def __aiter__(self) -> _AssertNoFurtherReads:
        return self

    async def __anext__(self) -> str:
        if not self._remaining:
            raise AssertionError("peek_for_content drained past the terminal-error chunk")
        return self._remaining.pop(0)


class _TrackingStream:
    """Same drain-sentinel as :class:`_AssertNoFurtherReads`, plus a real
    ``aclose`` that records whether it was ever called."""

    def __init__(self, chunks: list[str]) -> None:
        self._remaining = list(chunks)
        self.closed = False

    def __aiter__(self) -> _TrackingStream:
        return self

    async def __anext__(self) -> str:
        if not self._remaining:
            raise AssertionError("peek_for_content drained past the terminal-error chunk")
        return self._remaining.pop(0)

    async def aclose(self) -> None:
        self.closed = True


def test_error_stop_reason_breaks_before_message_stop() -> None:
    """A ``message_delta`` with ``stop_reason="error"`` breaks the drain
    the instant it arrives; the loop never asks for another chunk."""

    async def runner() -> PeekResult:
        return await peek_for_content(
            _AssertNoFurtherReads([_MESSAGE_START, _TERMINAL_ERROR_DELTA])
        )

    result = asyncio.run(runner())
    assert result.got_content is False
    assert result.stream_done is False


def test_disguised_error_text_breaks_before_message_stop() -> None:
    """The documented disguised-error text breaks the drain on the very
    chunk that carries it, before any terminal ``message_delta`` arrives."""

    async def runner() -> PeekResult:
        return await peek_for_content(_AssertNoFurtherReads([_FAKE_ERROR_TEXT_DELTA]))

    result = asyncio.run(runner())
    assert result.got_content is False
    assert result.stream_done is False
    assert result.looks_budget_starved is False


def test_early_exit_calls_aclose_on_the_abandoned_stream() -> None:
    """Primary, discriminating: on today's unmodified tree nothing calls
    ``aclose()`` (``closed`` stays ``False``); after the fix the abandoned
    stream is explicitly closed."""

    stream = _TrackingStream([_MESSAGE_START, _TERMINAL_ERROR_DELTA])

    async def runner() -> PeekResult:
        return await peek_for_content(stream)

    result = asyncio.run(runner())
    assert result.got_content is False
    assert stream.closed is True


def test_early_exit_tolerates_a_stream_without_aclose() -> None:
    """A hand-rolled test double lacking ``aclose`` entirely must not crash
    the early-exit path — the ``getattr(stream, "aclose", None)`` guard."""

    async def runner() -> PeekResult:
        return await peek_for_content(
            _AssertNoFurtherReads([_MESSAGE_START, _TERMINAL_ERROR_DELTA])
        )

    result = asyncio.run(runner())
    assert result.got_content is False
