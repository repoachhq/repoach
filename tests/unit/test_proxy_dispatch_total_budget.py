"""Unit tests for SP-CHAIN-REQUEST-BUDGET.

Pins the total wall-clock deadline that ``ClaudeProxyService.
_stream_with_failover`` now enforces across the WHOLE chain walk (not
per hop): the remaining budget shrinks as it is spent, a candidate
that would start after the budget is already gone is skipped without
ever being dispatched, ``claude_code`` is exempt from starvation, a
pure budget exhaustion raises a loud ``504`` with a per-hop breakdown,
and the thinking-budget retry (``_retry_with_more_budget``) is bound
by the same deadline.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import HTTPException

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
_MESSAGE_STOP = _sse("message_stop", {"type": "message_stop"})

_REAL_CONTENT = [
    _MESSAGE_START,
    _TEXT_CONTENT_START,
    _REAL_TEXT_DELTA,
    _CONTENT_BLOCK_STOP,
    _TERMINAL_REAL_DELTA,
    _MESSAGE_STOP,
]
_STARVED_EMPTY = [_MESSAGE_START, _TERMINAL_EMPTY_DELTA, _MESSAGE_STOP]


def test_settings_dispatch_total_budget_s_default_is_900() -> None:
    assert Settings(_env_file=None).dispatch_total_budget_s == 900.0


class _SleepThenServeProvider(BaseProvider):
    """Sleeps well past the configured budget, then would serve real content.

    Used to drive the OUTER ``asyncio.wait_for`` cutoff — the sleep never
    completes within ``effective_timeout`` so the caller must see a
    ``TimeoutError`` rather than genuinely waiting out the whole sleep.
    """

    SUPPORTS_NATIVE_TOOLS: bool = True

    def __init__(self, config: ProviderConfig, *, sleep_s: float) -> None:
        super().__init__(config)
        self._sleep_s = sleep_s
        self.calls: list[int | None] = []

    async def cleanup(self) -> None:
        return None

    async def stream_response(
        self, request: Any, input_tokens: int = 0, *, request_id: str | None = None
    ) -> AsyncIterator[str]:
        self.calls.append(request.max_tokens)
        await asyncio.sleep(self._sleep_s)
        for chunk in _REAL_CONTENT:
            yield chunk


class _ScriptedProvider(BaseProvider):
    """Replays fixed chunks instantly; records call count."""

    SUPPORTS_NATIVE_TOOLS: bool = True

    def __init__(self, config: ProviderConfig, *, chunks: list[str]) -> None:
        super().__init__(config)
        self._chunks = chunks
        self.calls: list[int | None] = []

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
    model_sonnet: str,
    dispatch_total_budget_s: float,
    claude_code_subprocess_timeout: float = 600.0,
) -> ClaudeProxyService:
    monkeypatch.setenv("MODEL", "nvidia_nim/z-ai/glm4.7")
    monkeypatch.setenv("MODEL_SONNET", model_sonnet)
    settings = Settings(_env_file=None)
    monkeypatch.setattr(settings, "dispatch_total_budget_s", dispatch_total_budget_s)
    monkeypatch.setattr(settings, "claude_code_subprocess_timeout", claude_code_subprocess_timeout)
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


def test_remaining_budget_preempts_a_hop_that_would_outlive_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sleeper hangs far past a tiny budget; the ``claude_code`` backstop
    still serves real content, and total elapsed stays a small fraction of
    the sleeper's own (deliberately huge) sleep duration.

    The sleeper's sleep is set far beyond the budget on purpose: since
    preemption cancels the hop via ``asyncio.wait_for`` long before the
    sleep would ever complete, inflating the sleep costs no real test
    time but buys a bound wide enough to absorb CPU scheduling jitter on
    a loaded CI runner without going anywhere near vacuous — only an
    actual loss of preemption (the hop running out its sleep) could
    breach it.
    """
    sleep_s = 60.0
    sleeper = _SleepThenServeProvider(ProviderConfig(api_key="x"), sleep_s=sleep_s)
    backstop = _ScriptedProvider(ProviderConfig(api_key="x"), chunks=_REAL_CONTENT)
    service = _build_service(
        monkeypatch,
        {"nvidia_nim": sleeper, "claude_code": backstop},
        model_sonnet="nvidia_nim/dispatch-budget-preempt-1,claude_code/sonnet",
        dispatch_total_budget_s=0.1,
        claude_code_subprocess_timeout=0.2,
    )

    started = time.monotonic()
    chunks = _drain(service.create_message(_make_request()))
    elapsed = time.monotonic() - started

    assert _REAL_TEXT_DELTA in chunks
    assert elapsed < sleep_s * 0.25, (
        f"expected bounded elapsed well under the {sleep_s}s sleep "
        f"(preemption should cut it to a small fraction of a second), took {elapsed}s"
    )
    assert backstop.calls == [128]


def test_exhausted_budget_skips_remaining_non_backstop_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three non-``claude_code`` candidates ; the first spends the whole
    tiny budget sleeping past its own ``effective_timeout``. The second
    and third must never be dispatched at all."""
    first = _SleepThenServeProvider(ProviderConfig(api_key="x"), sleep_s=5.0)
    second = _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["should not be reached"])
    third = _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["should not be reached"])
    service = _build_service(
        monkeypatch,
        {"nvidia_nim": first, "kimi": second, "groq": third},
        model_sonnet=(
            "nvidia_nim/dispatch-budget-skip-1,kimi/dispatch-budget-skip-2,"
            "groq/dispatch-budget-skip-3"
        ),
        dispatch_total_budget_s=0.1,
    )

    response = service.create_message(_make_request())
    with pytest.raises(HTTPException) as excinfo:
        _drain(response)

    assert excinfo.value.status_code == 504
    assert second.calls == []
    assert third.calls == []


def test_claude_code_backstop_is_never_starved_by_an_exhausted_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Budget already exhausted by an earlier hop ; ``claude_code`` still
    gets invoked and serves real content because its ``effective_timeout``
    is floored at ``claude_code_subprocess_timeout``."""
    sleeper = _SleepThenServeProvider(ProviderConfig(api_key="x"), sleep_s=5.0)
    backstop = _ScriptedProvider(ProviderConfig(api_key="x"), chunks=_REAL_CONTENT)
    service = _build_service(
        monkeypatch,
        {"nvidia_nim": sleeper, "claude_code": backstop},
        model_sonnet="nvidia_nim/dispatch-budget-floor-1,claude_code/sonnet",
        dispatch_total_budget_s=0.1,
        claude_code_subprocess_timeout=0.3,
    )

    chunks = _drain(service.create_message(_make_request()))

    assert _REAL_TEXT_DELTA in chunks
    assert backstop.calls == [128], "claude_code must still be dispatched despite exhausted budget"


def test_pure_budget_exhaustion_raises_504_with_per_hop_breakdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every candidate exhausted purely by the budget (no ``claude_code``
    in this chain) : a loud 504 with a structured per-hop breakdown."""
    first = _SleepThenServeProvider(ProviderConfig(api_key="x"), sleep_s=5.0)
    second = _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["unused"])
    service = _build_service(
        monkeypatch,
        {"nvidia_nim": first, "kimi": second},
        model_sonnet="nvidia_nim/dispatch-budget-504-1,kimi/dispatch-budget-504-2",
        dispatch_total_budget_s=0.1,
    )

    response = service.create_message(_make_request())
    with pytest.raises(HTTPException) as excinfo:
        _drain(response)

    assert excinfo.value.status_code == 504
    detail = excinfo.value.detail
    assert detail["error"] == "dispatch_budget_exhausted"
    hops = detail["hops"]
    assert len(hops) == 2
    for hop in hops:
        assert hop["reason"] in (
            "dispatch_budget_exhausted",
            "dispatch_budget_exhausted_before_dispatch",
        )
    assert second.calls == []


class _JumpingClock:
    """Deterministic fake for ``time.monotonic`` that jumps forward once.

    Every call returns a fixed ``base`` value until :meth:`jump` is
    invoked (simulating that a real dispatch attempt consumed wall
    clock), after which every subsequent call returns ``base +
    jump_by`` — comfortably past any budget computed from ``base``.
    Used to make "the deadline has already passed by the time the
    retry is considered" deterministic, without any real sleeping or
    timing races.
    """

    def __init__(self, base: float, jump_by: float) -> None:
        self._base = base
        self._jump_by = jump_by
        self._jumped = False

    def __call__(self) -> float:
        return self._base + self._jump_by if self._jumped else self._base

    def jump(self) -> None:
        self._jumped = True


def test_budget_retry_is_skipped_once_the_deadline_is_already_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A candidate returns a budget-starved empty completion, and by the
    time the retry would be issued the deadline has already passed (the
    attempt itself is modelled as having consumed the whole budget) :
    ``_retry_with_more_budget`` must return ``None`` without ever
    re-invoking the candidate's ``stream_response``, and the dispatcher
    fails over to the next candidate instead of retrying in place."""
    clock = _JumpingClock(base=1_000.0, jump_by=1_000.0)

    class _StarvedThenJumpProvider(BaseProvider):
        SUPPORTS_NATIVE_TOOLS: bool = True

        def __init__(self, config: ProviderConfig) -> None:
            super().__init__(config)
            self.calls: list[int | None] = []

        async def cleanup(self) -> None:
            return None

        async def stream_response(
            self, request: Any, input_tokens: int = 0, *, request_id: str | None = None
        ) -> AsyncIterator[str]:
            self.calls.append(request.max_tokens)
            clock.jump()
            for chunk in _STARVED_EMPTY:
                yield chunk

    head = _StarvedThenJumpProvider(ProviderConfig(api_key="x"))
    tail = _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["should not be reached"])
    service = _build_service(
        monkeypatch,
        {"nvidia_nim": head, "kimi": tail},
        model_sonnet="nvidia_nim/dispatch-budget-retry-skip-1,kimi/dispatch-budget-retry-skip-2",
        dispatch_total_budget_s=50.0,
    )
    monkeypatch.setattr("repoach.llm_proxy.api.services.time.monotonic", clock)

    response = service.create_message(_make_request())
    with pytest.raises(HTTPException):
        _drain(response)

    assert head.calls == [128], "retry must not re-invoke the same candidate a second time"
    assert tail.calls == [], "the next candidate is also past the exhausted deadline"
