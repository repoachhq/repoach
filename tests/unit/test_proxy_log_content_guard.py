"""Unit tests for SP-PROXY-LOG-CONTENT-GUARD.

``proxy_log_full_content`` gates two verbatim body-logging sites:
``FULL_PAYLOAD`` (``repoach.llm_proxy.api.services``) and
``SSE_EVENT`` (``repoach.llm_proxy.core.anthropic.sse``). At the
default (``False``) neither the request body nor an SSE event body
reaches the log — only non-content metadata does; with the flag
``True`` the legacy verbatim bodies are restored.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from loguru import logger as loguru_logger

from repoach.llm_proxy.api.models.anthropic import Message, MessagesRequest
from repoach.llm_proxy.api.services import ClaudeProxyService
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.core.anthropic.sse import SSEBuilder
from repoach.llm_proxy.providers.base import BaseProvider, ProviderConfig

_SECRET = "SECRET_TOKEN_zx91_do_not_log"


class _EchoProvider(BaseProvider):
    """Truthful boundary fake that streams a genuine SSE completion.

    Builds its response with the real :class:`SSEBuilder`, wired to
    the provider's own ``log_full_content`` config flag exactly like
    every production transport — no stubbing of proxy behavior.
    """

    def __init__(self, config: ProviderConfig, reply_text: str) -> None:
        super().__init__(config)
        self._reply_text = reply_text

    async def cleanup(self) -> None:
        return None

    async def stream_response(
        self,
        request: Any,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
    ) -> AsyncIterator[str]:
        sse = SSEBuilder(
            "msg_echo",
            request.model,
            input_tokens,
            log_full_content=self._config.log_full_content,
        )
        yield sse.message_start()
        for event in sse.ensure_text_block():
            yield event
        yield sse.emit_text_delta(self._reply_text)
        for event in sse.close_content_blocks():
            yield event
        yield sse.message_delta("end_turn", max(1, len(self._reply_text)))
        yield sse.message_stop()


def _capture_debug_logs() -> tuple[list[Any], int]:
    """Attach a loguru sink that records every DEBUG+ record's fields."""
    records: list[Any] = []
    sink_id = loguru_logger.add(
        lambda msg: records.append(msg.record),
        format="{message}",
        level="DEBUG",
    )
    return records, sink_id


def _request_carrying_secret() -> MessagesRequest:
    return MessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=32,
        messages=[Message(role="user", content=_SECRET)],
    )


def _build_service(
    monkeypatch: pytest.MonkeyPatch, *, log_full_content: bool
) -> ClaudeProxyService:
    monkeypatch.setenv("MODEL", "nvidia_nim/z-ai/glm4.7")
    monkeypatch.setenv("MODEL_SONNET", "nvidia_nim/meta/llama-3.3-70b-instruct")
    monkeypatch.setenv("REPOACH_PROXY_LOG_FULL_CONTENT", "true" if log_full_content else "false")
    settings = Settings()
    config = ProviderConfig(api_key="unused", log_full_content=log_full_content)
    provider = _EchoProvider(config, reply_text=f"the secret is {_SECRET}")
    return ClaudeProxyService(
        settings=settings,
        provider_getter=lambda provider_id: provider,
        token_counter=lambda *args, **kwargs: 0,
    )


def _drain(response: Any) -> list[str]:
    async def runner() -> list[str]:
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
        return chunks

    return asyncio.run(runner())


def test_default_off_no_bodies_in_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (``proxy_log_full_content=False``) — the guarded
    ``FULL_PAYLOAD`` and ``SSE_EVENT`` log lines carry no secret,
    even though the request/response genuinely contains one."""
    service = _build_service(monkeypatch, log_full_content=False)
    records, sink_id = _capture_debug_logs()
    try:
        response = service.create_message(_request_carrying_secret())
        _drain(response)
    finally:
        loguru_logger.remove(sink_id)

    full_payload_lines = [r["message"] for r in records if "FULL_PAYLOAD" in r["message"]]
    sse_event_lines = [r["message"] for r in records if "SSE_EVENT" in r["message"]]
    assert full_payload_lines, "expected at least one FULL_PAYLOAD log line"
    assert sse_event_lines, "expected at least one SSE_EVENT log line"
    assert not any(_SECRET in line for line in full_payload_lines), (
        f"secret leaked into a FULL_PAYLOAD line with the guard OFF: {full_payload_lines}"
    )
    assert not any(_SECRET in line for line in sse_event_lines), (
        f"secret leaked into an SSE_EVENT line with the guard OFF: {sse_event_lines}"
    )


def test_opt_in_logs_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``proxy_log_full_content=True`` the secret is present in
    both the ``FULL_PAYLOAD`` and the ``SSE_EVENT`` log lines."""
    service = _build_service(monkeypatch, log_full_content=True)
    records, sink_id = _capture_debug_logs()
    try:
        response = service.create_message(_request_carrying_secret())
        _drain(response)
    finally:
        loguru_logger.remove(sink_id)

    full_payload_lines = [r["message"] for r in records if "FULL_PAYLOAD" in r["message"]]
    sse_event_lines = [r["message"] for r in records if "SSE_EVENT" in r["message"]]
    assert any(_SECRET in line for line in full_payload_lines), (
        f"expected the secret in a FULL_PAYLOAD line with the guard ON, got {full_payload_lines}"
    )
    assert any(_SECRET in line for line in sse_event_lines), (
        f"expected the secret in an SSE_EVENT line with the guard ON, got {sse_event_lines}"
    )


def test_settings_default_proxy_log_full_content_is_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed default: an unset/misconfigured flag resolves to ``False``."""
    monkeypatch.delenv("REPOACH_PROXY_LOG_FULL_CONTENT", raising=False)
    monkeypatch.delenv("PROXY_LOG_FULL_CONTENT", raising=False)
    settings = Settings(_env_file=None)
    assert settings.proxy_log_full_content is False


def test_sse_builder_defaults_log_full_content_off() -> None:
    """:class:`SSEBuilder` constructed without the flag stays silent
    on event bodies (the constructor default mirrors the settings
    default)."""
    records, sink_id = _capture_debug_logs()
    try:
        sse = SSEBuilder("msg_1", "claude-sonnet-4-6", 0)
        sse.emit_text_delta(_SECRET)
    finally:
        loguru_logger.remove(sink_id)

    sse_lines = [r["message"] for r in records if "SSE_EVENT" in r["message"]]
    assert sse_lines, "expected at least one SSE_EVENT log line"
    assert not any(_SECRET in line for line in sse_lines)
