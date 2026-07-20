"""Unit tests for SP-PROXY-FAILOVER-TOOLLESS.

The proxy already failed over correctly for tools-using requests
(see ``tests/unit/test_proxy_chain_failover.py``).  This module
covers the symmetric path : tools-LESS requests — issued by every
reviewer persona, the analyst LLM client, and the bring-up smoke —
must now also walk the chain, peek for content, and fall over on
transport flaps, including the disguised "Connection error" text
shape that the PR #172 review run surfaced live.

Reproduces five contractual cases :

1. tools-less request + first candidate raises a transport-style
   exception → falls over to candidate 2.
2. tools-less request + first candidate emits the disguised
   ``Connection error.`` content_block_delta with
   ``output_tokens=0`` / ``stop_reason=error`` (the literal PR #172
   shape) → falls over to candidate 2.
3. tools-less request + first candidate yields real content → only
   one provider is instantiated, no failover loop.
4. tools-less request + every candidate fails → 502 surfaced with
   the chain length in the detail.
5. every request resolves the ONE universal chain that keeps
   ``claude_code/*`` as the last-resort backstop — there is no
   native-tools filter, so tools and tools-less requests are identical
   (SP-PROXY-UNIVERSAL-CHAIN).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.exceptions import HTTPException

from repoach.llm_proxy.api.model_router import ModelRouter
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
            "text": "Connection error. (request_id=req_def232f1cfca)",
        },
    },
)
_CONTENT_BLOCK_STOP = _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
_REAL_TEXT_DELTA = _sse(
    "content_block_delta",
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "APPROVE"},
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
        "usage": {"input_tokens": 10, "output_tokens": 0},
    },
)
_MESSAGE_STOP = _sse("message_stop", {"type": "message_stop"})


class _ScriptedProvider(BaseProvider):
    """Provider that either replays scripted SSE chunks or raises a
    scripted exception, with a call counter for assertions."""

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
        self.call_count = 0

    async def cleanup(self) -> None:
        return None

    async def stream_response(
        self,
        request: Any,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
    ) -> AsyncIterator[str]:
        self.call_count += 1
        if self._raises is not None:
            raise self._raises
        for chunk in self._chunks:
            yield chunk


def _toolless_request() -> MessagesRequest:
    return MessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=128,
        messages=[Message(role="user", content="ping")],
        tools=None,
    )


def _build_service(
    monkeypatch: pytest.MonkeyPatch,
    providers: dict[str, _ScriptedProvider],
    *,
    chain_env: str = "nvidia_nim/meta/llama-3.3-70b-instruct,kimi/kimi-k2.6",
) -> ClaudeProxyService:
    monkeypatch.setenv("MODEL", "nvidia_nim/z-ai/glm4.7")
    monkeypatch.setenv("MODEL_SONNET", chain_env)
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


def test_failover_on_transport_exception_when_no_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 1 — tools-less request + first candidate raises → candidate 2 serves."""
    providers = {
        "nvidia_nim": _ScriptedProvider(
            ProviderConfig(api_key="x"),
            raises=RuntimeError("simulated APIConnectionError"),
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
    }
    service = _build_service(monkeypatch, providers)

    response = service.create_message(_toolless_request())
    chunks = _drain(response)

    assert providers["nvidia_nim"].call_count == 1
    assert providers["kimi"].call_count == 1
    assert any(c == _TEXT_CONTENT_START for c in chunks)


def test_failover_on_disguised_text_error_when_no_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 2 — PR #172 SSE shape : candidate 1 emits a fake
    ``Connection error.`` content block followed by
    ``output_tokens=0`` / ``stop_reason=error``.  Candidate 2
    must serve and the disguised text must NEVER leak downstream."""
    providers = {
        "nvidia_nim": _ScriptedProvider(
            ProviderConfig(api_key="x"),
            chunks=[
                _MESSAGE_START,
                _EMPTY_TEXT_BLOCK_START,
                _FAKE_ERROR_TEXT_DELTA,
                _CONTENT_BLOCK_STOP,
                _TERMINAL_ERROR_DELTA,
                _MESSAGE_STOP,
            ],
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
    }
    service = _build_service(monkeypatch, providers)

    response = service.create_message(_toolless_request())
    chunks = _drain(response)

    assert providers["nvidia_nim"].call_count == 1
    assert providers["kimi"].call_count == 1
    assert any(c == _TEXT_CONTENT_START for c in chunks)
    assert _FAKE_ERROR_TEXT_DELTA not in chunks
    assert _TERMINAL_ERROR_DELTA not in chunks


def test_first_candidate_serves_no_extra_calls_when_no_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 3 — happy path : first candidate yields content, the
    second is never instantiated."""
    providers = {
        "nvidia_nim": _ScriptedProvider(
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
        "kimi": _ScriptedProvider(
            ProviderConfig(api_key="x"),
            chunks=["should-not-be-reached"],
        ),
    }
    service = _build_service(monkeypatch, providers)

    response = service.create_message(_toolless_request())
    chunks = _drain(response)

    assert providers["nvidia_nim"].call_count == 1
    assert providers["kimi"].call_count == 0
    assert any(c == _TEXT_CONTENT_START for c in chunks)
    assert "should-not-be-reached" not in chunks


def test_all_candidates_fail_re_raises_last_error_when_no_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 4a — every candidate raises an exception ; the service
    re-raises the last error (the ``raise last_error`` path of
    ``_stream_with_failover``, services.py:212-213)."""
    providers = {
        "nvidia_nim": _ScriptedProvider(
            ProviderConfig(api_key="x"),
            raises=RuntimeError("transport down 1"),
        ),
        "kimi": _ScriptedProvider(
            ProviderConfig(api_key="x"),
            raises=RuntimeError("transport down 2"),
        ),
    }
    service = _build_service(monkeypatch, providers)
    response = service.create_message(_toolless_request())

    with pytest.raises(RuntimeError, match="transport down 2"):
        _drain(response)

    assert providers["nvidia_nim"].call_count == 1
    assert providers["kimi"].call_count == 1


def test_all_candidates_empty_completion_raises_502_when_no_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 4b — every candidate yields an empty completion (no
    exception, ``output_tokens=0``) ; the service surfaces a 502 with
    the chain length in the detail (services.py:214-221, the
    ``raise HTTPException(502)`` path)."""
    providers = {
        "nvidia_nim": _ScriptedProvider(
            ProviderConfig(api_key="x"),
            chunks=[
                _MESSAGE_START,
                _EMPTY_TEXT_BLOCK_START,
                _FAKE_ERROR_TEXT_DELTA,
                _CONTENT_BLOCK_STOP,
                _TERMINAL_ERROR_DELTA,
                _MESSAGE_STOP,
            ],
        ),
        "kimi": _ScriptedProvider(
            ProviderConfig(api_key="x"),
            chunks=[
                _MESSAGE_START,
                _EMPTY_TEXT_BLOCK_START,
                _FAKE_ERROR_TEXT_DELTA,
                _CONTENT_BLOCK_STOP,
                _TERMINAL_ERROR_DELTA,
                _MESSAGE_STOP,
            ],
        ),
    }
    service = _build_service(monkeypatch, providers)
    response = service.create_message(_toolless_request())

    with pytest.raises(HTTPException) as exc_info:
        _drain(response)

    assert exc_info.value.status_code == 502
    assert "2 provider candidate" in str(exc_info.value.detail)
    assert providers["nvidia_nim"].call_count == 1
    assert providers["kimi"].call_count == 1


def test_resolve_chain_keeps_claude_code_backstop_universally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 5 — the one universal chain keeps ``claude_code/*`` as the
    last-resort backstop; with or without tools, every request gets the
    same full chain (the native-tools filter that used to drop it is
    gone)."""
    monkeypatch.setenv("MODEL", "nvidia_nim/z-ai/glm4.7")
    monkeypatch.setenv(
        "MODEL_SONNET",
        "nvidia_nim/meta/llama-3.3-70b-instruct,kimi/kimi-k2.6,claude_code/sonnet",
    )
    settings = Settings()
    router = ModelRouter(settings)

    chain = router.resolve_chain("claude-sonnet-4-6")
    refs = [c.provider_model_ref for c in chain]

    assert refs == [
        "nvidia_nim/meta/llama-3.3-70b-instruct",
        "kimi/kimi-k2.6",
        "claude_code/sonnet",
    ]
    assert chain[-1].provider_id == "claude_code"
