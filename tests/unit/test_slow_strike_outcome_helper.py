"""Unit tests for SP-SLOW-STRIKE-OUTCOME-DEDUP's shared completion-outcome helper.

Both the primary success path and the budget-retry success path of
:meth:`ClaudeProxyService._stream_with_failover` must route their
slow-strike completion bookkeeping through the single
:meth:`ClaudeProxyService._record_completion_outcome` helper instead of
running two divergent copy-pasted bodies. These tests monkeypatch the
helper with a recording spy and drive each success path through
``create_message``, reusing the ``_ScriptedProvider`` / ``_build_service``
harness pattern from ``tests/unit/test_slow_breaker_wiring.py``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from repoach.llm_proxy.api.models.anthropic import Message, MessagesRequest
from repoach.llm_proxy.api.services import ClaudeProxyService
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.base import BaseProvider, ProviderConfig
from repoach.llm_proxy.routing import reset_breaker


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
    """Provider whose ``stream_response`` replays scripted SSE chunks."""

    SUPPORTS_NATIVE_TOOLS: bool = True

    def __init__(
        self,
        config: ProviderConfig,
        *,
        chunks: list[str] | None = None,
    ) -> None:
        super().__init__(config)
        self._chunks = chunks or []

    async def cleanup(self) -> None:
        return None

    async def stream_response(
        self,
        request: Any,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
    ) -> AsyncIterator[str]:
        for chunk in self._chunks:
            yield chunk


class _BudgetThenSuccessProvider(BaseProvider):
    """Budget-starved empty completion on the first call; the enlarged-budget
    retry call succeeds with real content."""

    SUPPORTS_NATIVE_TOOLS: bool = True

    def __init__(self, config: ProviderConfig, *, threshold: int) -> None:
        super().__init__(config)
        self._threshold = threshold
        self.calls: list[int | None] = []

    async def cleanup(self) -> None:
        return None

    async def stream_response(
        self,
        request: Any,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
    ) -> AsyncIterator[str]:
        self.calls.append(request.max_tokens)
        if request.max_tokens is None or request.max_tokens < self._threshold:
            for chunk in _EMPTY_CONTENT_CHUNKS:
                yield chunk
            return
        for chunk in _REAL_CONTENT_CHUNKS:
            yield chunk


def _make_request() -> MessagesRequest:
    return MessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=128,
        messages=[Message(role="user", content="ping")],
    )


def _build_service(
    monkeypatch: pytest.MonkeyPatch,
    providers: dict[str, Any],
    *,
    slow_latency_gate_s: float = 999.0,
    slow_tps_floor: float = 1.0,
) -> ClaudeProxyService:
    monkeypatch.setenv("MODEL", "nvidia_nim/z-ai/glm4.7")
    monkeypatch.setenv(
        "MODEL_SONNET",
        "nvidia_nim/meta/llama-3.3-70b-instruct,kimi/kimi-k2.6,groq/llama-3.3-70b-versatile",
    )
    monkeypatch.setenv("REPOACH_BREAKER_SLOW_SHADOW", "true")
    monkeypatch.setenv("REPOACH_BREAKER_SLOW_LATENCY_GATE_S", str(slow_latency_gate_s))
    monkeypatch.setenv("REPOACH_BREAKER_SLOW_TPS_FLOOR", str(slow_tps_floor))
    monkeypatch.setenv("REPOACH_BREAKER_SLOW_K", "1")
    monkeypatch.setenv("REPOACH_BREAKER_SLOW_N", "5")
    monkeypatch.setenv("REPOACH_BREAKER_SLOW_TTL_S", "300.0")
    monkeypatch.setenv("REPOACH_BREAKER_ENABLED", "true")
    monkeypatch.setenv("REPOACH_PROXY_BUDGET_RETRY_ENABLED", "true")
    settings = Settings(_env_file=None)
    return ClaudeProxyService(
        settings=settings,
        provider_getter=lambda provider_id: providers[provider_id],
        token_counter=lambda *args, **kwargs: 0,
    )


def _drain_stream_response(response: Any) -> list[str]:
    async def runner() -> list[str]:
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
        return chunks

    return asyncio.run(runner())


def _install_recording_spy(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _spy(
        self_arg: ClaudeProxyService,
        ref: Any,
        latency_s: float,
        output_tokens: int | None,
        **kwargs: Any,
    ) -> None:
        calls.append(
            {
                "ref": ref,
                "latency_s": latency_s,
                "output_tokens": output_tokens,
                **kwargs,
            }
        )

    monkeypatch.setattr(ClaudeProxyService, "_record_completion_outcome", _spy)
    return calls


def _drive_primary_success(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls = _install_recording_spy(monkeypatch)
    providers = {
        "nvidia_nim": _ScriptedProvider(ProviderConfig(api_key="x"), chunks=_REAL_CONTENT_CHUNKS),
        "kimi": _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["unused"]),
        "groq": _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["unused"]),
    }
    service = _build_service(monkeypatch, providers)
    response = service.create_message(_make_request())
    _drain_stream_response(response)
    return calls


def _drive_budget_retry_success(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls = _install_recording_spy(monkeypatch)
    provider = _BudgetThenSuccessProvider(ProviderConfig(api_key="x"), threshold=200)
    providers = {
        "nvidia_nim": provider,
        "kimi": _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["unused"]),
        "groq": _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["unused"]),
    }
    service = _build_service(monkeypatch, providers)
    response = service.create_message(_make_request())
    _drain_stream_response(response)
    assert provider.calls == [128, 1024]
    return calls


def test_primary_success_path_calls_shared_outcome_helper_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On today's tree ``_record_completion_outcome`` does not exist, so
    ``monkeypatch.setattr`` raises ``AttributeError`` and this fails
    immediately; after the fix, the primary success path routes through
    the shared helper exactly once with ``budget_retry=False`` and
    ``attempt_index=0``."""
    calls = _drive_primary_success(monkeypatch)

    assert len(calls) == 1
    assert calls[0]["budget_retry"] is False
    assert calls[0]["attempt_index"] == 0


def test_budget_retry_success_path_calls_shared_outcome_helper_with_budget_retry_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget-retry success path calls the shared helper exactly once
    (never for the budget-starved first attempt) with
    ``budget_retry=True``."""
    calls = _drive_budget_retry_success(monkeypatch)

    assert len(calls) == 1
    assert calls[0]["budget_retry"] is True
    assert calls[0]["attempt_index"] == 0


def test_helper_call_signature_is_identical_across_both_sites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both call sites route through one shared parameter contract:
    the kwarg key set the spy observes at the primary success site and
    at the budget-retry success site are identical."""
    primary_calls = _drive_primary_success(monkeypatch)
    retry_calls = _drive_budget_retry_success(monkeypatch)

    assert len(primary_calls) == 1
    assert len(retry_calls) == 1

    primary_keys = set(primary_calls[0].keys())
    retry_keys = set(retry_calls[0].keys())
    assert primary_keys == retry_keys
    assert primary_keys == {
        "ref",
        "latency_s",
        "output_tokens",
        "dispatch_id",
        "request_id",
        "candidate",
        "attempt_index",
        "prior_failures",
        "budget_retry",
    }
