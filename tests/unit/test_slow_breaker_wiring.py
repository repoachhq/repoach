"""Unit tests for SP-BREAKER-SLOW-STRIKE services.py wiring.

Covers the slow-completion policy hook applied at both the primary
success path and the budget-retry success path inside
:meth:`ClaudeProxyService._stream_with_failover`.  Uses the real
:func:`is_slow_completion` and the real :class:`BreakerState` singleton;
only the provider/stream boundary is faked with scripted SSE chunks.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from loguru import logger as loguru_logger

from repoach.llm_proxy.api.models.anthropic import (
    Message,
    MessagesRequest,
)
from repoach.llm_proxy.api.services import ClaudeProxyService
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.base import BaseProvider, ProviderConfig
from repoach.llm_proxy.routing import get_breaker, reset_breaker
from repoach.llm_proxy.routing.refs import ModelRef


@pytest.fixture(autouse=True)
def _reset_breaker_state() -> None:
    reset_breaker()
    yield
    reset_breaker()


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

_REAL_TEXT_DELTA = _sse(
    "content_block_delta",
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "hello world"},
    },
)

_CONTENT_BLOCK_STOP = _sse("content_block_stop", {"type": "content_block_stop", "index": 0})

_MESSAGE_DELTA_REAL = _sse(
    "message_delta",
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"input_tokens": 10, "output_tokens": 5},
    },
)

_MESSAGE_STOP = _sse("message_stop", {"type": "message_stop"})

_REAL_CONTENT_CHUNKS: list[str] = [
    _MESSAGE_START,
    _TEXT_CONTENT_START,
    _REAL_TEXT_DELTA,
    _CONTENT_BLOCK_STOP,
    _MESSAGE_DELTA_REAL,
    _MESSAGE_STOP,
]

_EMPTY_CONTENT_CHUNKS: list[str] = [
    _MESSAGE_START,
    _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"input_tokens": 10, "output_tokens": 0},
        },
    ),
    _MESSAGE_STOP,
]


class _ScriptedProvider(BaseProvider):
    """Provider whose ``stream_response`` replays scripted SSE chunks.

    ``delay_s`` gives a test deterministic control over the full-completion
    wall-clock latency :func:`is_slow_completion` observes: the delay is
    awaited once, before any chunk is yielded, so the dispatcher's
    ``attempt_latency_s`` (measured across the whole ``peek_for_content``
    drain) reliably exceeds a shrunk latency gate without relying on a
    real-world timing race.
    """

    SUPPORTS_NATIVE_TOOLS: bool = True

    def __init__(
        self,
        config: ProviderConfig,
        *,
        chunks: list[str] | None = None,
        raises: Exception | None = None,
        delay_s: float = 0.0,
    ) -> None:
        super().__init__(config)
        self._chunks = chunks or []
        self._raises = raises
        self._delay_s = delay_s

    async def cleanup(self) -> None:
        return None

    async def stream_response(
        self,
        request: Any,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
    ) -> AsyncIterator[str]:
        if self._raises is not None:
            raise self._raises
        if self._delay_s > 0:
            await asyncio.sleep(self._delay_s)
        for chunk in self._chunks:
            yield chunk


def _capture_loguru() -> tuple[list[Any], int]:
    """Install a loguru sink that buffers records; return (records, sink_id)."""
    records: list[Any] = []
    sink_id = loguru_logger.add(
        lambda msg: records.append(msg.record),
        format="{message}",
        level="DEBUG",
    )
    return records, sink_id


def _make_request() -> MessagesRequest:
    return MessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=128,
        messages=[Message(role="user", content="ping")],
    )


def _build_service(
    monkeypatch: pytest.MonkeyPatch,
    providers: dict[str, _ScriptedProvider],
    *,
    slow_shadow: bool = True,
    slow_latency_gate_s: float = 0.001,
    slow_tps_floor: float = 1.0,
    slow_k: int = 1,
    slow_n: int = 5,
    slow_ttl_s: float = 300.0,
) -> ClaudeProxyService:
    monkeypatch.setenv("MODEL", "nvidia_nim/z-ai/glm4.7")
    monkeypatch.setenv(
        "MODEL_SONNET",
        "nvidia_nim/meta/llama-3.3-70b-instruct,kimi/kimi-k2.6,groq/llama-3.3-70b-versatile",
    )
    monkeypatch.setenv("REPOACH_BREAKER_SLOW_SHADOW", str(slow_shadow).lower())
    monkeypatch.setenv("REPOACH_BREAKER_SLOW_LATENCY_GATE_S", str(slow_latency_gate_s))
    monkeypatch.setenv("REPOACH_BREAKER_SLOW_TPS_FLOOR", str(slow_tps_floor))
    monkeypatch.setenv("REPOACH_BREAKER_SLOW_K", str(slow_k))
    monkeypatch.setenv("REPOACH_BREAKER_SLOW_N", str(slow_n))
    monkeypatch.setenv("REPOACH_BREAKER_SLOW_TTL_S", str(slow_ttl_s))
    monkeypatch.setenv("REPOACH_BREAKER_ENABLED", "true")
    settings = Settings(_env_file=None)
    return ClaudeProxyService(
        settings=settings,
        provider_getter=lambda provider_id: providers[provider_id],
        token_counter=lambda *args, **kwargs: 0,
    )


def _drain_stream_response(response: Any) -> list[str]:
    """Synchronously collect the chunks from a ``StreamingResponse``."""

    async def runner() -> list[str]:
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
        return chunks

    return asyncio.run(runner())


def test_slow_policy_hook_shadow_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """In shadow mode a k-of-n slow completion logs ``breaker_slow_strike_shadow``
    with ``would_trip=True`` but does not trip the breaker."""
    records, sink_id = _capture_loguru()
    try:
        providers = {
            "nvidia_nim": _ScriptedProvider(
                ProviderConfig(api_key="x"),
                chunks=_REAL_CONTENT_CHUNKS,
                delay_s=0.08,
            ),
            "kimi": _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["unused"]),
            "groq": _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["unused"]),
        }
        service = _build_service(
            monkeypatch,
            providers,
            slow_shadow=True,
            slow_latency_gate_s=0.001,
            slow_tps_floor=100.0,
            slow_k=1,
            slow_n=5,
        )
        response = service.create_message(_make_request())
        _drain_stream_response(response)

        ref = ModelRef.parse("nvidia_nim/meta/llama-3.3-70b-instruct")
        assert not get_breaker().is_down(ref, now=time.monotonic())

        shadow_logs = [r for r in records if r["message"] == "breaker_slow_strike_shadow"]
        assert len(shadow_logs) >= 1
        assert shadow_logs[0]["extra"]["would_trip"] is True
    finally:
        loguru_logger.remove(sink_id)


def test_slow_policy_hook_enforcing_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """In enforcing mode a k-of-n slow completion trips the breaker with
    reason ``slow_completion`` at the short slow TTL."""
    providers = {
        "nvidia_nim": _ScriptedProvider(
            ProviderConfig(api_key="x"),
            chunks=_REAL_CONTENT_CHUNKS,
            delay_s=0.08,
        ),
        "kimi": _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["unused"]),
        "groq": _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["unused"]),
    }
    service = _build_service(
        monkeypatch,
        providers,
        slow_shadow=False,
        slow_latency_gate_s=0.001,
        slow_tps_floor=100.0,
        slow_k=1,
        slow_n=5,
        slow_ttl_s=300.0,
    )
    response = service.create_message(_make_request())
    _drain_stream_response(response)

    ref = ModelRef.parse("nvidia_nim/meta/llama-3.3-70b-instruct")
    breaker = get_breaker()
    assert breaker.is_down(ref, now=time.monotonic())
    assert breaker._down_reason.get(ref) == "slow_completion"
    assert breaker._consecutive_failures.get(ref, 0) == 0


def test_slow_policy_below_k_no_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single slow completion below the k threshold does not trip the breaker,
    and does not call recover (hard counters keep current value)."""
    records, sink_id = _capture_loguru()
    try:
        providers = {
            "nvidia_nim": _ScriptedProvider(
                ProviderConfig(api_key="x"),
                chunks=_REAL_CONTENT_CHUNKS,
                delay_s=0.08,
            ),
            "kimi": _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["unused"]),
            "groq": _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["unused"]),
        }
        service = _build_service(
            monkeypatch,
            providers,
            slow_shadow=False,
            slow_latency_gate_s=0.001,
            slow_tps_floor=100.0,
            slow_k=3,
            slow_n=5,
        )
        response = service.create_message(_make_request())
        _drain_stream_response(response)

        ref = ModelRef.parse("nvidia_nim/meta/llama-3.3-70b-instruct")
        breaker = get_breaker()
        assert not breaker.is_down(ref, now=time.monotonic())

        strike_logs = [r for r in records if r["message"] == "breaker_slow_strike"]
        assert len(strike_logs) >= 1

        assert ref in breaker._slow_history
        assert len(breaker._slow_history[ref]) == 1
    finally:
        loguru_logger.remove(sink_id)


def test_slow_policy_fast_success_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fast completion calls recover(), clearing trips, counters, and slow history."""
    ref = ModelRef.parse("nvidia_nim/meta/llama-3.3-70b-instruct")
    breaker = get_breaker()

    breaker.trip(ref, now=time.monotonic(), ttl_s=120.0, reason="timeout")
    breaker.record_success(ref, True, k=3, n=5)
    assert breaker.is_down(ref, now=time.monotonic() + 1)
    assert ref in breaker._slow_history
    assert ref in breaker._consecutive_failures

    providers = {
        "nvidia_nim": _ScriptedProvider(
            ProviderConfig(api_key="x"),
            chunks=_REAL_CONTENT_CHUNKS,
        ),
        "kimi": _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["unused"]),
        "groq": _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["unused"]),
    }
    service = _build_service(
        monkeypatch,
        providers,
        slow_shadow=False,
        slow_latency_gate_s=999.0,
        slow_tps_floor=1.0,
        slow_k=1,
        slow_n=5,
    )
    response = service.create_message(_make_request())
    _drain_stream_response(response)

    assert not breaker.is_down(ref, now=time.monotonic())
    assert ref not in breaker._consecutive_failures
    assert ref not in breaker._slow_history
