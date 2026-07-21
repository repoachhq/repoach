"""Unit tests for SP-BREAKER-SLOW-STRIKE.

Covers two things: the pure ``is_slow_completion`` gate/floor policy
(:mod:`repoach.llm_proxy.routing.slow_policy`), and that
:class:`PeekResult` exposes the ``usage.output_tokens`` from the final
``message_delta`` emitted by an SSE stream, so the slow-completion
policy in the services layer can compute tokens-per-second without
re-parsing the buffered events.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator
from typing import Any

import pytest

from repoach.llm_proxy.api._failover import PeekResult, peek_for_content
from repoach.llm_proxy.routing.slow_policy import is_slow_completion


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


def test_is_slow_below_gate_returns_false() -> None:
    """A completion at or under the latency gate is never slow, no matter
    how thin its tokens-per-second is."""
    assert is_slow_completion(5.0, 1, gate_s=10.0, tps_floor=1.0) is False
    assert is_slow_completion(10.0, 1, gate_s=10.0, tps_floor=1.0) is False


def test_is_slow_slow_when_above_gate_and_low_tps() -> None:
    """Past the gate with thin tokens-per-second is a slow-completion strike."""
    assert is_slow_completion(15.0, 7, gate_s=10.0, tps_floor=1.0) is True


def test_is_slow_none_tokens_returns_false() -> None:
    """Unknown output tokens is conservative: never strike blind."""
    assert is_slow_completion(30.0, None, gate_s=10.0, tps_floor=1.0) is False


def test_is_slow_boundaries() -> None:
    """Exact gate is not slow; equal tps to the floor is not slow (strict
    inequalities on both sides)."""
    assert is_slow_completion(10.0, 0, gate_s=10.0, tps_floor=1.0) is False
    assert is_slow_completion(10.0001, 1000, gate_s=10.0, tps_floor=1.0) is False
    assert is_slow_completion(10.0001, 9, gate_s=10.0, tps_floor=1.0) is True
    assert is_slow_completion(10.0001, 5, gate_s=10.0, tps_floor=1.0) is True


@pytest.mark.parametrize(
    "latency_s,output_tokens,gate_s,tps_floor",
    [
        (0.0, 0, 0.0, 1.0),
        (-5.0, 3, 10.0, 1.0),
        (1e12, 1, 1.0, 1.0),
        (0.0, None, -1.0, 1.0),
        (math.inf, 5, 10.0, 1.0),
        (10.0, 0, -10.0, 0.0),
    ],
)
def test_is_slow_never_raises(
    latency_s: float, output_tokens: int | None, gate_s: float, tps_floor: float
) -> None:
    """The policy is total: no exotic finite/edge input raises."""
    result = is_slow_completion(latency_s, output_tokens, gate_s=gate_s, tps_floor=tps_floor)
    assert isinstance(result, bool)
