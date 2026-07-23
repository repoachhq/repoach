"""Unit tests for SP-PROXY-CHAIN-FAILOVER.

Cover three layers:

1. The pure peek helpers in :mod:`repoach.llm_proxy.api._failover`.
2. ``ModelRouter.resolve_chain`` returning the filtered ordered chain.
3. ``ClaudeProxyService._stream_with_failover`` driving the chain walk
   end-to-end with mocked providers.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from repoach.llm_proxy.api._failover import (
    PeekResult,
    chunk_is_tool_use_start,
    chunk_message_delta_stop_reason,
    chunk_message_delta_usage,
    chunk_text_delta,
    peek_for_content,
)
from repoach.llm_proxy.api.model_router import ModelRouter
from repoach.llm_proxy.api.models.anthropic import (
    Message,
    MessagesRequest,
    Tool,
)
from repoach.llm_proxy.api.services import ClaudeProxyService
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.base import BaseProvider, ProviderConfig

# ---------------------------------------------------------------------------
# SSE chunk fixtures
# ---------------------------------------------------------------------------


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
_EMPTY_TEXT_BLOCK_START = _sse(
    "content_block_start",
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
)
_FAKE_ERROR_TEXT_DELTA = _sse(
    "content_block_delta",
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {
            "type": "text_delta",
            "text": "Connection error. (request_id=req_3ee92997627d)",
        },
    },
)
_CONTENT_BLOCK_STOP = _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
_TERMINAL_EMPTY_DELTA = _sse(
    "message_delta",
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"input_tokens": 10, "output_tokens": 0},
    },
)
_TERMINAL_REAL_DELTA = _sse(
    "message_delta",
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"input_tokens": 10, "output_tokens": 12},
    },
)
_TERMINAL_ERROR_DELTA = _sse(
    "message_delta",
    {
        "type": "message_delta",
        "delta": {"stop_reason": "error", "stop_sequence": None},
        "usage": {"input_tokens": 10, "output_tokens": 5},
    },
)
_MESSAGE_STOP = _sse("message_stop", {"type": "message_stop"})


# ---------------------------------------------------------------------------
# Layer 1 — peek helpers
# ---------------------------------------------------------------------------


def test_chunk_is_tool_use_start_recognises_tool_use_only() -> None:
    assert chunk_is_tool_use_start(_TOOL_USE_START) is True
    assert chunk_is_tool_use_start(_TEXT_CONTENT_START) is False
    assert chunk_is_tool_use_start(_MESSAGE_START) is False


def test_chunk_message_delta_usage_returns_none_for_non_message_delta() -> None:
    assert chunk_message_delta_usage(_MESSAGE_START) is None
    assert chunk_message_delta_usage(_TEXT_CONTENT_START) is None


def test_chunk_message_delta_usage_extracts_payload() -> None:
    usage = chunk_message_delta_usage(_TERMINAL_REAL_DELTA)
    assert usage is not None
    assert usage.get("output_tokens") == 12

    usage = chunk_message_delta_usage(_TERMINAL_EMPTY_DELTA)
    assert usage is not None
    assert usage.get("output_tokens") == 0


def test_chunk_message_delta_stop_reason() -> None:
    assert chunk_message_delta_stop_reason(_TERMINAL_REAL_DELTA) == "end_turn"
    assert chunk_message_delta_stop_reason(_TERMINAL_ERROR_DELTA) == "error"
    assert chunk_message_delta_stop_reason(_MESSAGE_START) is None


async def _aiter(chunks: list[str]) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


def test_peek_for_content_recognises_tool_use_response() -> None:
    async def runner() -> PeekResult:
        return await peek_for_content(
            _aiter([_MESSAGE_START, _TOOL_USE_START, _TERMINAL_REAL_DELTA, _MESSAGE_STOP])
        )

    result = asyncio.run(runner())
    assert result.got_content is True
    assert result.stream_done is True


def test_peek_for_content_recognises_real_text_response_via_token_count() -> None:
    async def runner() -> PeekResult:
        return await peek_for_content(
            _aiter(
                [
                    _MESSAGE_START,
                    _TEXT_CONTENT_START,
                    _REAL_TEXT_DELTA,
                    _CONTENT_BLOCK_STOP,
                    _TERMINAL_REAL_DELTA,
                    _MESSAGE_STOP,
                ]
            )
        )

    result = asyncio.run(runner())
    assert result.got_content is True


def test_peek_for_content_treats_zero_output_tokens_as_failure() -> None:
    async def runner() -> PeekResult:
        return await peek_for_content(
            _aiter([_MESSAGE_START, _TERMINAL_EMPTY_DELTA, _MESSAGE_STOP])
        )

    result = asyncio.run(runner())
    assert result.got_content is False
    assert result.stream_done is True


def test_peek_for_content_treats_fake_error_text_as_failure() -> None:
    """SP-PROXY-CHAIN-FAILOVER-V2: NIM disguises connection errors as a
    text content block; the final ``message_delta.usage.output_tokens=0``
    is the authoritative failure signal even when synthetic text was
    streamed first."""

    async def runner() -> PeekResult:
        return await peek_for_content(
            _aiter(
                [
                    _MESSAGE_START,
                    _EMPTY_TEXT_BLOCK_START,
                    _FAKE_ERROR_TEXT_DELTA,
                    _CONTENT_BLOCK_STOP,
                    _TERMINAL_EMPTY_DELTA,
                    _MESSAGE_STOP,
                ]
            )
        )

    result = asyncio.run(runner())
    assert result.got_content is False
    assert result.stream_done is False


def test_peek_for_content_treats_error_stop_reason_as_failure() -> None:
    async def runner() -> PeekResult:
        return await peek_for_content(
            _aiter([_MESSAGE_START, _TERMINAL_ERROR_DELTA, _MESSAGE_STOP])
        )

    result = asyncio.run(runner())
    assert result.got_content is False


_WHITESPACE_TEXT_DELTA = _sse(
    "content_block_delta",
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": " "},
    },
)
_EMPTY_TEXT_DELTA = _sse(
    "content_block_delta",
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": ""},
    },
)
_REAL_TEXT_DELTA = _sse(
    "content_block_delta",
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "APPROVE"},
    },
)
_TERMINAL_ONE_TOKEN_DELTA = _sse(
    "message_delta",
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"input_tokens": 10, "output_tokens": 1},
    },
)


def test_chunk_text_delta_extracts_text_payload() -> None:
    assert chunk_text_delta(_REAL_TEXT_DELTA) == "APPROVE"
    assert chunk_text_delta(_WHITESPACE_TEXT_DELTA) == " "
    assert chunk_text_delta(_EMPTY_TEXT_DELTA) == ""
    assert chunk_text_delta(_TEXT_CONTENT_START) is None
    assert chunk_text_delta(_TERMINAL_REAL_DELTA) is None
    assert chunk_text_delta(_MESSAGE_STOP) is None


def test_chunk_text_delta_returns_none_when_text_is_not_string() -> None:
    """Defensive — the ``isinstance(text, str)`` guard must not let
    a non-string payload (integer, None, list) leak through as a
    truthy text value.  Some malformed providers have been observed
    emitting ``"text": null`` or ``"text": 0`` on transport flake."""
    non_string_payload = _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": 42},
        },
    )
    null_text_payload = _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": None},
        },
    )
    missing_text_payload = _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta"},
        },
    )
    assert chunk_text_delta(non_string_payload) is None
    assert chunk_text_delta(null_text_payload) is None
    assert chunk_text_delta(missing_text_payload) is None


def test_peek_for_content_accumulates_multiple_whitespace_chunks_as_failure() -> None:
    """Multiple whitespace-only text_delta chunks must accumulate +
    strip to ``""`` so the failover trigger fires regardless of how
    the upstream split its non-content tokens across SSE chunks
    (``" "`` + ``" "`` + ``"\\t"``, etc.).
    """
    tab_text_delta = _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "\t"},
        },
    )
    terminal_three_tokens = _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"input_tokens": 10, "output_tokens": 3},
        },
    )

    async def runner() -> PeekResult:
        return await peek_for_content(
            _aiter(
                [
                    _MESSAGE_START,
                    _TEXT_CONTENT_START,
                    _WHITESPACE_TEXT_DELTA,
                    _WHITESPACE_TEXT_DELTA,
                    tab_text_delta,
                    _CONTENT_BLOCK_STOP,
                    terminal_three_tokens,
                    _MESSAGE_STOP,
                ]
            )
        )

    result = asyncio.run(runner())
    assert result.got_content is False
    assert result.stream_done is True


def test_peek_for_content_treats_whitespace_only_text_as_failure() -> None:
    """SP-PROXY-FAILOVER-WHITESPACE — Kimi K2.6 pattern.

    The upstream emits a single space character with
    ``output_tokens=1``.  Under the previous rule that classified as
    success (token count > 0) ; the new rule requires at least one
    non-whitespace character in the accumulated ``text_delta``.
    """

    async def runner() -> PeekResult:
        return await peek_for_content(
            _aiter(
                [
                    _MESSAGE_START,
                    _TEXT_CONTENT_START,
                    _WHITESPACE_TEXT_DELTA,
                    _CONTENT_BLOCK_STOP,
                    _TERMINAL_ONE_TOKEN_DELTA,
                    _MESSAGE_STOP,
                ]
            )
        )

    result = asyncio.run(runner())
    assert result.got_content is False
    assert result.stream_done is True


def test_peek_for_content_treats_empty_text_with_positive_tokens_as_failure() -> None:
    """SP-PROXY-FAILOVER-WHITESPACE edge case.

    Some upstreams emit a billable token with an empty ``text_delta``
    payload.  Accumulating to ``""`` strips to ``""`` and must
    trigger failover.
    """

    async def runner() -> PeekResult:
        return await peek_for_content(
            _aiter(
                [
                    _MESSAGE_START,
                    _TEXT_CONTENT_START,
                    _EMPTY_TEXT_DELTA,
                    _CONTENT_BLOCK_STOP,
                    _TERMINAL_ONE_TOKEN_DELTA,
                    _MESSAGE_STOP,
                ]
            )
        )

    result = asyncio.run(runner())
    assert result.got_content is False


def test_peek_for_content_recognises_real_text_via_text_delta() -> None:
    """Non-regression — a real word reaches got_content=True."""

    async def runner() -> PeekResult:
        return await peek_for_content(
            _aiter(
                [
                    _MESSAGE_START,
                    _TEXT_CONTENT_START,
                    _REAL_TEXT_DELTA,
                    _CONTENT_BLOCK_STOP,
                    _TERMINAL_REAL_DELTA,
                    _MESSAGE_STOP,
                ]
            )
        )

    result = asyncio.run(runner())
    assert result.got_content is True


def test_peek_for_content_tool_use_wins_over_whitespace_text() -> None:
    """A tool_use start short-circuits the whitespace check.

    Defensive — the failover rule must not penalise an upstream that
    emitted a tool_use block and happens to include a whitespace
    text_delta alongside it.
    """

    async def runner() -> PeekResult:
        return await peek_for_content(
            _aiter(
                [
                    _MESSAGE_START,
                    _TOOL_USE_START,
                    _WHITESPACE_TEXT_DELTA,
                    _TERMINAL_ONE_TOKEN_DELTA,
                    _MESSAGE_STOP,
                ]
            )
        )

    result = asyncio.run(runner())
    assert result.got_content is True


# ---------------------------------------------------------------------------
# Layer 2 — ModelRouter.resolve_chain
# ---------------------------------------------------------------------------


@pytest.fixture()
def chained_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("MODEL", "nvidia_nim/z-ai/glm4.7")
    monkeypatch.setenv(
        "MODEL_SONNET",
        "claude_code/sonnet,nvidia_nim/meta/llama-3.3-70b-instruct,kimi/kimi-k2.6",
    )
    return Settings()


def test_resolve_chain_returns_full_configured_chain(chained_settings: Settings) -> None:
    """One universal chain — claude_code stays in it as the backstop,
    with or without tools (the native-tools filter is gone)."""
    router = ModelRouter(chained_settings)
    chain = router.resolve_chain("claude-sonnet-4-6")

    assert len(chain) == 3
    assert chain[0].provider_id == "claude_code"
    assert chain[1].provider_id == "nvidia_nim"
    assert chain[2].provider_id == "kimi"


# ---------------------------------------------------------------------------
# Layer 3 — service-level chain walking
# ---------------------------------------------------------------------------


class _ScriptedProvider(BaseProvider):
    """Provider whose ``stream_response`` replays scripted SSE chunks
    or raises a scripted exception."""

    SUPPORTS_NATIVE_TOOLS: bool = True

    def __init__(
        self,
        config: ProviderConfig,
        *,
        chunks: list[str] | None = None,
        raises: Exception | None = None,
    ) -> None:
        super().__init__(config)
        self._chunks = chunks or []
        self._raises = raises

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
        for chunk in self._chunks:
            yield chunk


def _make_request(*, with_tools: bool = True) -> MessagesRequest:
    tools = (
        [
            Tool(
                name="send_whatsapp",
                description="Send a WhatsApp message",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        ]
        if with_tools
        else None
    )
    return MessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=128,
        messages=[Message(role="user", content="ping")],
        tools=tools,
    )


def _build_service(
    monkeypatch: pytest.MonkeyPatch,
    providers: dict[str, _ScriptedProvider],
) -> ClaudeProxyService:
    monkeypatch.setenv("MODEL", "nvidia_nim/z-ai/glm4.7")
    monkeypatch.setenv(
        "MODEL_SONNET",
        "nvidia_nim/meta/llama-3.3-70b-instruct,kimi/kimi-k2.6,groq/llama-3.3-70b-versatile",
    )
    settings = Settings()
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


def test_first_candidate_serves_when_it_yields_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers = {
        "nvidia_nim": _ScriptedProvider(
            ProviderConfig(api_key="x"),
            chunks=[_MESSAGE_START, _TOOL_USE_START, _TERMINAL_REAL_DELTA, _MESSAGE_STOP],
        ),
        "kimi": _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["should not be reached"]),
        "groq": _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["should not be reached"]),
    }
    service = _build_service(monkeypatch, providers)

    response = service.create_message(_make_request())
    chunks = _drain_stream_response(response)

    assert _TOOL_USE_START in chunks
    assert "should not be reached" not in chunks


def test_failover_on_transport_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    providers = {
        "nvidia_nim": _ScriptedProvider(
            ProviderConfig(api_key="x"),
            raises=RuntimeError("simulated transport error"),
        ),
        "kimi": _ScriptedProvider(
            ProviderConfig(api_key="x"),
            chunks=[
                _MESSAGE_START,
                _TEXT_CONTENT_START,
                _REAL_TEXT_DELTA,
                _CONTENT_BLOCK_STOP,
                _TERMINAL_REAL_DELTA,
                _MESSAGE_STOP,
            ],
        ),
        "groq": _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["unused"]),
    }
    service = _build_service(monkeypatch, providers)

    response = service.create_message(_make_request())
    chunks = _drain_stream_response(response)

    assert any(c == _TEXT_CONTENT_START for c in chunks)
    assert "unused" not in chunks


def test_failover_on_empty_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    providers = {
        "nvidia_nim": _ScriptedProvider(
            ProviderConfig(api_key="x"),
            chunks=[_MESSAGE_START, _TERMINAL_EMPTY_DELTA, _MESSAGE_STOP],
        ),
        "kimi": _ScriptedProvider(
            ProviderConfig(api_key="x"),
            chunks=[_MESSAGE_START, _TOOL_USE_START, _TERMINAL_REAL_DELTA, _MESSAGE_STOP],
        ),
        "groq": _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["unused"]),
    }
    service = _build_service(monkeypatch, providers)

    response = service.create_message(_make_request())
    chunks = _drain_stream_response(response)

    assert any(c == _TOOL_USE_START for c in chunks)
    # The buffered events from the failed nvidia_nim attempt must not
    # leak into the successful response.
    assert _TERMINAL_EMPTY_DELTA not in chunks


def test_failover_on_fake_error_text_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    """SP-PROXY-CHAIN-FAILOVER-V2: NIM disguises connection errors as a
    text content block; the dispatcher must still fail over because
    ``message_delta.usage.output_tokens`` is zero."""
    providers = {
        "nvidia_nim": _ScriptedProvider(
            ProviderConfig(api_key="x"),
            chunks=[
                _MESSAGE_START,
                _EMPTY_TEXT_BLOCK_START,
                _FAKE_ERROR_TEXT_DELTA,
                _CONTENT_BLOCK_STOP,
                _TERMINAL_EMPTY_DELTA,
                _MESSAGE_STOP,
            ],
        ),
        "kimi": _ScriptedProvider(
            ProviderConfig(api_key="x"),
            chunks=[_MESSAGE_START, _TOOL_USE_START, _TERMINAL_REAL_DELTA, _MESSAGE_STOP],
        ),
        "groq": _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["unused"]),
    }
    service = _build_service(monkeypatch, providers)

    response = service.create_message(_make_request())
    chunks = _drain_stream_response(response)

    assert any(c == _TOOL_USE_START for c in chunks)
    assert _FAKE_ERROR_TEXT_DELTA not in chunks


def test_failover_on_whitespace_only_text_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    """SP-PROXY-FAILOVER-WHITESPACE end-to-end.

    Kimi K2.6 pattern observed on PR #175 Sentinel pass : the upstream
    emits a single space character with ``output_tokens=1``.  The
    previous rule (``output_tokens > 0`` → success) let that empty
    response reach the reviewer's parser, which then classified it as
    TRANSPORT and emitted a parse_failed verdict.  The new rule
    triggers chain failover so the next candidate gets a chance.
    """
    providers = {
        "nvidia_nim": _ScriptedProvider(
            ProviderConfig(api_key="x"),
            chunks=[
                _MESSAGE_START,
                _TEXT_CONTENT_START,
                _WHITESPACE_TEXT_DELTA,
                _CONTENT_BLOCK_STOP,
                _TERMINAL_ONE_TOKEN_DELTA,
                _MESSAGE_STOP,
            ],
        ),
        "kimi": _ScriptedProvider(
            ProviderConfig(api_key="x"),
            chunks=[_MESSAGE_START, _TOOL_USE_START, _TERMINAL_REAL_DELTA, _MESSAGE_STOP],
        ),
        "groq": _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["unused"]),
    }
    service = _build_service(monkeypatch, providers)

    response = service.create_message(_make_request())
    chunks = _drain_stream_response(response)

    assert any(c == _TOOL_USE_START for c in chunks)
    assert _WHITESPACE_TEXT_DELTA not in chunks


def test_all_candidates_failing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    providers = {
        "nvidia_nim": _ScriptedProvider(
            ProviderConfig(api_key="x"),
            chunks=[_MESSAGE_START, _TERMINAL_EMPTY_DELTA, _MESSAGE_STOP],
        ),
        "kimi": _ScriptedProvider(ProviderConfig(api_key="x"), raises=RuntimeError("kimi down")),
        "groq": _ScriptedProvider(
            ProviderConfig(api_key="x"),
            chunks=[_MESSAGE_START, _TERMINAL_EMPTY_DELTA, _MESSAGE_STOP],
        ),
    }
    service = _build_service(monkeypatch, providers)

    response = service.create_message(_make_request())
    with pytest.raises(Exception) as excinfo:
        _drain_stream_response(response)
    # Either the last raised exception, or the synthesised 502 if none
    # raised.  In this scenario kimi raised RuntimeError which is the
    # last_error captured at the end of the walk.
    msg = str(excinfo.value)
    assert "kimi down" in msg or "502" in msg or "empty completions" in msg
