"""Integration test for SP-PROXY-LOG-CONTENT-GUARD AC2.

Drives a real request end-to-end through the proxy FastAPI
``TestClient``. The registered provider is a truthful boundary fake
that fetches its completion body over ``httpx.MockTransport`` (the
network edge) and streams the reply back with the production
:class:`SSEBuilder` — exactly like every real transport. With default
settings (``proxy_log_full_content=False``, no env override), no
loguru record captured for the whole request carries the request
message body or the system-prompt text.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from loguru import logger as loguru_logger

from repoach.llm_proxy.api.app import create_app
from repoach.llm_proxy.config.settings import get_settings
from repoach.llm_proxy.core.anthropic.sse import SSEBuilder
from repoach.llm_proxy.providers.base import BaseProvider, ProviderConfig
from repoach.llm_proxy.providers.registry import ProviderRegistry
from repoach.llm_proxy.routing import reset_breaker

_SYSTEM_PROMPT_SECRET = "SYS_PROMPT_do_not_log_9f2c"
_USER_MESSAGE_SECRET = "USER_MSG_do_not_log_7ab1"


class _MockTransportProvider(BaseProvider):
    """Boundary fake — fetches its completion body over ``httpx.MockTransport``
    (the genuine network edge for a provider), then streams it back to
    the proxy with a real :class:`SSEBuilder`, wired to this provider's
    own ``log_full_content`` config exactly like production transports."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)

    async def cleanup(self) -> None:
        return None

    @staticmethod
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"reply": "The answer is 42."})

    async def stream_response(
        self,
        request: Any,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
    ) -> AsyncIterator[str]:
        transport = httpx.MockTransport(self._handler)
        async with httpx.AsyncClient(transport=transport) as client:
            upstream = await client.post(
                "https://mock-provider.test/v1/complete",
                json={"messages": ["ping"]},
            )
        reply_text = upstream.json()["reply"]

        sse = SSEBuilder(
            "msg_mock",
            request.model,
            input_tokens,
            log_full_content=self._config.log_full_content,
        )
        yield sse.message_start()
        for event in sse.ensure_text_block():
            yield event
        yield sse.emit_text_delta(reply_text)
        for event in sse.close_content_blocks():
            yield event
        yield sse.message_delta("end_turn", max(1, len(reply_text)))
        yield sse.message_stop()


@pytest.fixture()
def captured_loguru() -> list[Any]:
    """Capture every loguru record emitted during the test body."""
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


def _post_request(client: TestClient, headers: dict[str, str]) -> httpx.Response:
    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 32,
        "system": _SYSTEM_PROMPT_SECRET,
        "messages": [{"role": "user", "content": _USER_MESSAGE_SECRET}],
    }
    return client.post("/v1/messages", json=body, headers=headers)


def test_default_settings_leave_no_body_or_system_prompt_in_log(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    captured_loguru: list[Any],
) -> None:
    """Default posture (guard OFF): the request body / system prompt
    text never reaches any captured log record, yet the guarded log
    lines still fire (metadata-only)."""
    reset_breaker()
    monkeypatch.setenv("REPOACH_ANTHROPIC_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("REPOACH_PROXY_DEFAULT_MODEL", "nvidia_nim/good-model")
    monkeypatch.setenv("MODEL_SONNET", "nvidia_nim/good-model")
    monkeypatch.delenv("REPOACH_PROXY_LOG_FULL_CONTENT", raising=False)
    monkeypatch.delenv("PROXY_LOG_FULL_CONTENT", raising=False)
    get_settings.cache_clear()
    request.addfinalizer(get_settings.cache_clear)
    settings = get_settings()
    monkeypatch.setattr(settings, "anthropic_auth_token", "test-token")
    assert settings.proxy_log_full_content is False

    app = create_app()
    provider = _MockTransportProvider(ProviderConfig(api_key="x", log_full_content=False))
    registry = ProviderRegistry({"nvidia_nim": provider})

    headers = {"x-api-key": "test-token"}
    with TestClient(app) as client:
        app.state.provider_registry = registry
        resp = _post_request(client, headers)

    assert resp.status_code == 200, resp.text
    assert "The answer is 42." in resp.text

    all_messages = [r["message"] for r in captured_loguru]
    assert not any(_SYSTEM_PROMPT_SECRET in m for m in all_messages), (
        "system prompt text leaked into a log record with the default (OFF) guard"
    )
    assert not any(_USER_MESSAGE_SECRET in m for m in all_messages), (
        "request message body leaked into a log record with the default (OFF) guard"
    )

    full_payload_lines = [m for m in all_messages if "FULL_PAYLOAD" in m]
    sse_event_lines = [m for m in all_messages if "SSE_EVENT" in m]
    assert full_payload_lines, "expected at least one FULL_PAYLOAD log line"
    assert sse_event_lines, "expected at least one SSE_EVENT log line"


def test_opt_in_settings_carry_body_and_system_prompt_in_log(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    captured_loguru: list[Any],
) -> None:
    """Operator opt-in (guard ON): the legacy verbatim bodies return."""
    reset_breaker()
    monkeypatch.setenv("REPOACH_ANTHROPIC_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("REPOACH_PROXY_DEFAULT_MODEL", "nvidia_nim/good-model")
    monkeypatch.setenv("MODEL_SONNET", "nvidia_nim/good-model")
    monkeypatch.setenv("REPOACH_PROXY_LOG_FULL_CONTENT", "true")
    get_settings.cache_clear()
    request.addfinalizer(get_settings.cache_clear)
    settings = get_settings()
    monkeypatch.setattr(settings, "anthropic_auth_token", "test-token")
    assert settings.proxy_log_full_content is True

    app = create_app()
    provider = _MockTransportProvider(ProviderConfig(api_key="x", log_full_content=True))
    registry = ProviderRegistry({"nvidia_nim": provider})

    headers = {"x-api-key": "test-token"}
    with TestClient(app) as client:
        app.state.provider_registry = registry
        resp = _post_request(client, headers)

    assert resp.status_code == 200, resp.text

    all_messages = [r["message"] for r in captured_loguru]
    full_payload_lines = [m for m in all_messages if "FULL_PAYLOAD" in m]
    sse_event_lines = [m for m in all_messages if "SSE_EVENT" in m]
    assert any(_SYSTEM_PROMPT_SECRET in m for m in full_payload_lines), (
        f"expected the system prompt in a FULL_PAYLOAD line with the guard ON, got {full_payload_lines}"
    )
    assert any(_USER_MESSAGE_SECRET in m for m in full_payload_lines), (
        f"expected the user message in a FULL_PAYLOAD line with the guard ON, got {full_payload_lines}"
    )
    assert any("The answer is 42." in m for m in sse_event_lines), (
        f"expected the reply text in an SSE_EVENT line with the guard ON, got {sse_event_lines}"
    )
