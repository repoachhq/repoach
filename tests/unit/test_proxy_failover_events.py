"""Smoke tests pinning the SP-LLM-PROXY-FAILOVER-LOG structured events.

When ``ClaudeProxyService._stream_with_failover`` walks the chain and
falls back from one candidate to the next, it must emit a
structured ``proxy_chain_failover_fired`` event so the operator can
alert on per-hour fallback frequency. When the chain is fully
exhausted without producing a usable completion, a single
``proxy_chain_exhausted`` ERROR event must precede the HTTP error.

The proxy uses ``loguru`` rather than structlog, so the tests
install a small sink that buffers structured records for assertion.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import HTTPException
from loguru import logger as loguru_logger

from ferova.llm_proxy.api.models.anthropic import Message, MessagesRequest, Tool
from ferova.llm_proxy.api.services import ClaudeProxyService
from ferova.llm_proxy.config.settings import Settings
from ferova.llm_proxy.providers.base import BaseProvider, ProviderConfig


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
_TEXT_BLOCK_START = _sse(
    "content_block_start",
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
)
_TEXT_DELTA_REAL = _sse(
    "content_block_delta",
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "hello"},
    },
)
_CONTENT_BLOCK_STOP = _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
_TERMINAL_DELTA_REAL = _sse(
    "message_delta",
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"input_tokens": 10, "output_tokens": 7},
    },
)
_MESSAGE_STOP = _sse("message_stop", {"type": "message_stop"})
_TERMINAL_DELTA_EMPTY = _sse(
    "message_delta",
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"input_tokens": 10, "output_tokens": 0},
    },
)


class _ScriptedProvider(BaseProvider):
    """Provider that replays scripted SSE chunks or raises a scripted exception."""

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


def _make_request() -> MessagesRequest:
    return MessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=64,
        messages=[Message(role="user", content="ping")],
        tools=[
            Tool(
                name="send_whatsapp",
                description="x",
                input_schema={"type": "object", "properties": {}, "required": []},
            )
        ],
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


def _drain(response: Any) -> list[str]:
    async def runner() -> list[str]:
        out: list[str] = []
        async for chunk in response.body_iterator:
            out.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
        return out

    return asyncio.run(runner())


@pytest.fixture()
def captured_loguru() -> list[Any]:
    """Capture loguru records for the duration of a test.

    Returns a mutable list whose entries are ``loguru.Record`` dicts;
    each entry exposes ``["message"]`` (the event name) plus an
    ``["extra"]`` dict carrying the structured kwargs we passed via
    ``logger.warning("event", key=value)``.
    """
    records: list[Any] = []
    sink_id = loguru_logger.add(
        lambda msg: records.append(msg.record),
        format="{message}",
        level="DEBUG",
    )
    try:
        yield records
    finally:
        loguru_logger.remove(sink_id)


def _events(records: list[Any]) -> list[str]:
    return [r["message"] for r in records]


def _find(records: list[Any], event: str) -> Any:
    for r in records:
        if r["message"] == event:
            return r
    raise AssertionError(f"event {event!r} not in {_events(records)}")


class TestPrimaryReasonClassifier:
    """``_classify_failover_reason`` maps exception types to spec vocabulary."""

    def test_timeout_class_returns_timeout(self) -> None:
        from ferova.llm_proxy.api.services import _classify_failover_reason

        class _ReadTimeoutError(Exception):
            pass

        assert _classify_failover_reason(_ReadTimeoutError("slow upstream")) == "timeout"

    def test_rate_limit_returns_rate_limited(self) -> None:
        from ferova.llm_proxy.api.services import _classify_failover_reason
        from ferova.llm_proxy.providers.exceptions import RateLimitError

        assert _classify_failover_reason(RateLimitError("429")) == "rate_limited"

    def test_authentication_returns_auth_failed(self) -> None:
        from ferova.llm_proxy.api.services import _classify_failover_reason
        from ferova.llm_proxy.providers.exceptions import AuthenticationError

        assert _classify_failover_reason(AuthenticationError("bad token")) == "auth_failed"

    def test_transport_returns_transport_error(self) -> None:
        from ferova.llm_proxy.api.services import _classify_failover_reason

        class _RemoteProtocolError(Exception):
            pass

        assert _classify_failover_reason(_RemoteProtocolError("disconnect")) == "transport_error"

    def test_unclassified_falls_back_to_exception_prefix(self) -> None:
        from ferova.llm_proxy.api.services import _classify_failover_reason

        assert _classify_failover_reason(RuntimeError("oops")) == "exception:RuntimeError"


class TestProxyChainFailoverFired:
    """``proxy_chain_failover_fired`` fires on each failover step."""

    def test_event_fires_on_transport_exception(
        self,
        monkeypatch: pytest.MonkeyPatch,
        captured_loguru: list[Any],
    ) -> None:
        providers = {
            "nvidia_nim": _ScriptedProvider(
                ProviderConfig(api_key="x"),
                raises=RuntimeError("simulated transport error"),
            ),
            "kimi": _ScriptedProvider(
                ProviderConfig(api_key="x"),
                chunks=[
                    _MESSAGE_START,
                    _TEXT_BLOCK_START,
                    _TEXT_DELTA_REAL,
                    _CONTENT_BLOCK_STOP,
                    _TERMINAL_DELTA_REAL,
                    _MESSAGE_STOP,
                ],
            ),
            "groq": _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["unused"]),
        }
        service = _build_service(monkeypatch, providers)
        response = service.create_message(_make_request())
        _drain(response)

        fired = _find(captured_loguru, "proxy_chain_failover_fired")
        extra = fired["extra"]
        assert extra["primary_reason"] == "exception:RuntimeError"
        assert extra["attempt"] == 1
        assert extra["chain_remaining"] >= 1
        assert "primary" in extra
        assert isinstance(extra["latency_s"], (int, float))
        assert extra["latency_s"] >= 0.0

    def test_event_fires_on_empty_completion(
        self,
        monkeypatch: pytest.MonkeyPatch,
        captured_loguru: list[Any],
    ) -> None:
        providers = {
            "nvidia_nim": _ScriptedProvider(
                ProviderConfig(api_key="x"),
                chunks=[_MESSAGE_START, _TERMINAL_DELTA_EMPTY, _MESSAGE_STOP],
            ),
            "kimi": _ScriptedProvider(
                ProviderConfig(api_key="x"),
                chunks=[
                    _MESSAGE_START,
                    _TEXT_BLOCK_START,
                    _TEXT_DELTA_REAL,
                    _CONTENT_BLOCK_STOP,
                    _TERMINAL_DELTA_REAL,
                    _MESSAGE_STOP,
                ],
            ),
            "groq": _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["unused"]),
        }
        service = _build_service(monkeypatch, providers)
        response = service.create_message(_make_request())
        _drain(response)

        fired = _find(captured_loguru, "proxy_chain_failover_fired")
        assert fired["extra"]["primary_reason"] == "empty_completion"
        assert isinstance(fired["extra"]["latency_s"], (int, float))
        assert fired["extra"]["latency_s"] >= 0.0

    def test_recovered_event_fires_when_fallback_succeeds(
        self,
        monkeypatch: pytest.MonkeyPatch,
        captured_loguru: list[Any],
    ) -> None:
        providers = {
            "nvidia_nim": _ScriptedProvider(
                ProviderConfig(api_key="x"),
                raises=RuntimeError("primary down"),
            ),
            "kimi": _ScriptedProvider(
                ProviderConfig(api_key="x"),
                chunks=[
                    _MESSAGE_START,
                    _TEXT_BLOCK_START,
                    _TEXT_DELTA_REAL,
                    _CONTENT_BLOCK_STOP,
                    _TERMINAL_DELTA_REAL,
                    _MESSAGE_STOP,
                ],
            ),
            "groq": _ScriptedProvider(ProviderConfig(api_key="x"), chunks=["unused"]),
        }
        service = _build_service(monkeypatch, providers)
        response = service.create_message(_make_request())
        _drain(response)

        recovered = _find(captured_loguru, "proxy_chain_failover_recovered")
        assert recovered["extra"]["attempt"] == 2
        assert recovered["extra"]["earlier_failures"] == 1
        assert isinstance(recovered["extra"]["latency_s"], (int, float))
        assert recovered["extra"]["latency_s"] >= 0.0


class TestProxyChainExhausted:
    """``proxy_chain_exhausted`` fires once when no candidate produces content."""

    def test_event_fires_and_records_failures(
        self,
        monkeypatch: pytest.MonkeyPatch,
        captured_loguru: list[Any],
    ) -> None:
        providers = {
            "nvidia_nim": _ScriptedProvider(
                ProviderConfig(api_key="x"),
                raises=RuntimeError("nim down"),
            ),
            "kimi": _ScriptedProvider(
                ProviderConfig(api_key="x"),
                raises=RuntimeError("kimi down"),
            ),
            "groq": _ScriptedProvider(
                ProviderConfig(api_key="x"),
                raises=RuntimeError("groq down"),
            ),
        }
        service = _build_service(monkeypatch, providers)

        with pytest.raises((RuntimeError, HTTPException)):
            response = service.create_message(_make_request())
            _drain(response)

        exhausted = _find(captured_loguru, "proxy_chain_exhausted")
        extra = exhausted["extra"]
        assert extra["attempts"] >= 2
        assert len(extra["failures"]) >= 2
        assert all(isinstance(f[1], str) for f in extra["failures"])

    def test_event_fires_with_mixed_failure_types(
        self,
        monkeypatch: pytest.MonkeyPatch,
        captured_loguru: list[Any],
    ) -> None:
        """Exhausted event must carry a heterogeneous ``failures`` list.

        The first candidate raises (transport-style), the second
        completes with an empty completion (peek-style), the third
        raises again. The exhausted event lists all three with their
        per-attempt classified reason.
        """
        providers = {
            "nvidia_nim": _ScriptedProvider(
                ProviderConfig(api_key="x"),
                raises=RuntimeError("nim transport"),
            ),
            "kimi": _ScriptedProvider(
                ProviderConfig(api_key="x"),
                chunks=[_MESSAGE_START, _TERMINAL_DELTA_EMPTY, _MESSAGE_STOP],
            ),
            "groq": _ScriptedProvider(
                ProviderConfig(api_key="x"),
                raises=RuntimeError("groq down"),
            ),
        }
        service = _build_service(monkeypatch, providers)

        with pytest.raises((RuntimeError, HTTPException)):
            response = service.create_message(_make_request())
            _drain(response)

        fired = [r for r in captured_loguru if r["message"] == "proxy_chain_failover_fired"]
        reasons = {r["extra"]["primary_reason"] for r in fired}
        assert "empty_completion" in reasons
        assert any(
            reason.startswith("exception:") or reason == "transport_error" for reason in reasons
        )

        exhausted = _find(captured_loguru, "proxy_chain_exhausted")
        extra = exhausted["extra"]
        assert extra["attempts"] >= 2
        failure_reasons = {reason for _ref, reason in extra["failures"]}
        assert "empty_completion" in failure_reasons
