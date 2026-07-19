"""Unit tests for SP-PROXY-THINKING-BUDGET-RETRY.

A thinking model whose hidden reasoning consumes a tight ``max_tokens``
budget returns an empty completion that ``peek_for_content`` flags as
``looks_budget_starved``. The dispatcher retries the SAME candidate once
with an enlarged budget before failing over — so a capable model that
only needed headroom is not discarded as dead.

These tests drive ``ClaudeProxyService._stream_with_failover`` with a
provider whose output depends on the request's ``max_tokens`` and that
records every call, so the retry control-flow is observable.
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
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "HELLO"}},
)
_CONTENT_BLOCK_STOP = _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
_TERMINAL_REAL_DELTA = _sse(
    "message_delta",
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"input_tokens": 10, "output_tokens": 12},
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

_STARVED_EMPTY = [_MESSAGE_START, _TERMINAL_EMPTY_DELTA, _MESSAGE_STOP]
_ERROR_EMPTY = [_MESSAGE_START, _TERMINAL_ERROR_DELTA, _MESSAGE_STOP]
_REAL_CONTENT = [
    _MESSAGE_START,
    _TEXT_CONTENT_START,
    _REAL_TEXT_DELTA,
    _CONTENT_BLOCK_STOP,
    _TERMINAL_REAL_DELTA,
    _MESSAGE_STOP,
]


class _BudgetSensitiveProvider(BaseProvider):
    """Yields ``below`` chunks under ``threshold`` max_tokens, ``at_or_above``
    chunks otherwise, and records each call's ``max_tokens``."""

    SUPPORTS_NATIVE_TOOLS: bool = True

    def __init__(
        self,
        config: ProviderConfig,
        *,
        threshold: int,
        below: list[str],
        at_or_above: list[str],
    ) -> None:
        super().__init__(config)
        self._threshold = threshold
        self._below = below
        self._at_or_above = at_or_above
        self.calls: list[int] = []

    async def cleanup(self) -> None:
        return None

    async def stream_response(
        self, request: Any, input_tokens: int = 0, *, request_id: str | None = None
    ) -> AsyncIterator[str]:
        self.calls.append(request.max_tokens)
        chunks = self._at_or_above if request.max_tokens >= self._threshold else self._below
        for chunk in chunks:
            yield chunk


class _ScriptedProvider(BaseProvider):
    """Replays fixed chunks regardless of budget; records call count."""

    SUPPORTS_NATIVE_TOOLS: bool = True

    def __init__(self, config: ProviderConfig, *, chunks: list[str]) -> None:
        super().__init__(config)
        self._chunks = chunks
        self.calls: list[int] = []

    async def cleanup(self) -> None:
        return None

    async def stream_response(
        self, request: Any, input_tokens: int = 0, *, request_id: str | None = None
    ) -> AsyncIterator[str]:
        self.calls.append(request.max_tokens)
        for chunk in self._chunks:
            yield chunk


def _make_request() -> MessagesRequest:
    return MessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=128,
        messages=[Message(role="user", content="ping")],
        tools=None,
    )


def _build_service(
    monkeypatch: pytest.MonkeyPatch,
    providers: dict[str, BaseProvider],
    *,
    retry_enabled: bool = True,
) -> ClaudeProxyService:
    monkeypatch.setenv("MODEL", "nvidia_nim/z-ai/glm4.7")
    monkeypatch.setenv("MODEL_SONNET", "nvidia_nim/qwen/thinker,kimi/kimi-k2.6")
    monkeypatch.setenv("BUDGET_RETRY_ENABLED", "true" if retry_enabled else "false")
    settings = Settings()
    return ClaudeProxyService(
        settings=settings,
        provider_getter=lambda provider_id: providers[provider_id],
        token_counter=lambda *args, **kwargs: 0,
    )


def _drain(response: Any) -> list[str]:
    async def runner() -> list[str]:
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
        return chunks

    return asyncio.run(runner())


def test_budget_starved_empty_retries_same_candidate_with_more_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head = _BudgetSensitiveProvider(
        ProviderConfig(api_key="x"), threshold=500, below=_STARVED_EMPTY, at_or_above=_REAL_CONTENT
    )
    tail = _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["should not be reached"])
    service = _build_service(monkeypatch, {"nvidia_nim": head, "kimi": tail})

    chunks = _drain(service.create_message(_make_request()))

    assert _REAL_TEXT_DELTA in chunks
    assert "should not be reached" not in chunks
    assert head.calls == [128, 1024]
    assert tail.calls == []


def test_dead_candidate_fails_over_after_one_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    head = _BudgetSensitiveProvider(
        ProviderConfig(api_key="x"),
        threshold=10**9,
        below=_STARVED_EMPTY,
        at_or_above=_REAL_CONTENT,
    )
    tail = _ScriptedProvider(ProviderConfig(api_key="x"), chunks=_REAL_CONTENT)
    service = _build_service(monkeypatch, {"nvidia_nim": head, "kimi": tail})

    chunks = _drain(service.create_message(_make_request()))

    assert _REAL_TEXT_DELTA in chunks
    assert head.calls == [128, 1024]
    assert tail.calls == [128]


def test_non_starved_empty_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    head = _BudgetSensitiveProvider(
        ProviderConfig(api_key="x"), threshold=500, below=_ERROR_EMPTY, at_or_above=_REAL_CONTENT
    )
    tail = _ScriptedProvider(ProviderConfig(api_key="x"), chunks=_REAL_CONTENT)
    service = _build_service(monkeypatch, {"nvidia_nim": head, "kimi": tail})

    chunks = _drain(service.create_message(_make_request()))

    assert _REAL_TEXT_DELTA in chunks
    assert head.calls == [128]
    assert tail.calls == [128]


def test_disabled_flag_keeps_immediate_failover(monkeypatch: pytest.MonkeyPatch) -> None:
    head = _BudgetSensitiveProvider(
        ProviderConfig(api_key="x"), threshold=500, below=_STARVED_EMPTY, at_or_above=_REAL_CONTENT
    )
    tail = _ScriptedProvider(ProviderConfig(api_key="x"), chunks=_REAL_CONTENT)
    service = _build_service(monkeypatch, {"nvidia_nim": head, "kimi": tail}, retry_enabled=False)

    chunks = _drain(service.create_message(_make_request()))

    assert _REAL_TEXT_DELTA in chunks
    assert head.calls == [128]
    assert tail.calls == [128]


class _SequencedProvider(BaseProvider):
    """Plays one chunk list per call, repeating the last; records budgets."""

    SUPPORTS_NATIVE_TOOLS: bool = True

    def __init__(self, config: ProviderConfig, *, per_call: list[list[str]]) -> None:
        super().__init__(config)
        self._per_call = per_call
        self.calls: list[int | None] = []

    async def cleanup(self) -> None:
        return None

    async def stream_response(
        self, request: Any, input_tokens: int = 0, *, request_id: str | None = None
    ) -> AsyncIterator[str]:
        index = min(len(self.calls), len(self._per_call) - 1)
        self.calls.append(request.max_tokens)
        for chunk in self._per_call[index]:
            yield chunk


def _make_request_with_budget(max_tokens: int | None) -> MessagesRequest:
    return MessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=[Message(role="user", content="ping")],
        tools=None,
    )


def test_none_max_tokens_starved_empty_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request without max_tokens survives a starved empty and retries.

    _retry_with_more_budget multiplied original_max unguarded; a None
    max_tokens on the starved path raised TypeError outside any try.
    The escalation now bases itself on the cap when no original ask
    exists.
    """
    head = _SequencedProvider(ProviderConfig(api_key="x"), per_call=[_STARVED_EMPTY, _REAL_CONTENT])
    tail = _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["should not be reached"])
    service = _build_service(monkeypatch, {"nvidia_nim": head, "kimi": tail})

    chunks = _drain(service.create_message(_make_request_with_budget(None)))

    assert _REAL_TEXT_DELTA in chunks
    assert head.calls == [None, 8192]
    assert tail.calls == []


def test_escalation_exceeds_the_effective_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A first attempt at the 4096 answer-headroom floor retries strictly above.

    With the old 4096 default cap the enlarged ask equalled the
    effective floor of the combined-budget providers and the retry was
    a provable no-op; the 8192 default gives the x8 factor real room.
    """
    head = _BudgetSensitiveProvider(
        ProviderConfig(api_key="x"), threshold=8000, below=_STARVED_EMPTY, at_or_above=_REAL_CONTENT
    )
    tail = _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["should not be reached"])
    service = _build_service(monkeypatch, {"nvidia_nim": head, "kimi": tail})

    chunks = _drain(service.create_message(_make_request_with_budget(4096)))

    assert _REAL_TEXT_DELTA in chunks
    assert head.calls == [4096, 8192]
    assert tail.calls == []


def test_at_cap_requests_still_fail_over_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A starved empty already at the cap fails over immediately."""
    head = _BudgetSensitiveProvider(
        ProviderConfig(api_key="x"),
        threshold=10**9,
        below=_STARVED_EMPTY,
        at_or_above=_REAL_CONTENT,
    )
    tail = _ScriptedProvider(ProviderConfig(api_key="x"), chunks=_REAL_CONTENT)
    service = _build_service(monkeypatch, {"nvidia_nim": head, "kimi": tail})

    chunks = _drain(service.create_message(_make_request_with_budget(8192)))

    assert _REAL_TEXT_DELTA in chunks
    assert head.calls == [8192]
    assert tail.calls == [8192]
