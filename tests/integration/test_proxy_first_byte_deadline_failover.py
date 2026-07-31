"""Integration test for SP-PROXY-FIRST-BYTE-DEADLINE.

End-to-end: a chain whose FIRST hop accepts the request and never
emits a single SSE chunk fails over to the second, healthy hop well
under ``http_read_timeout`` — bounded instead by the new, much
tighter ``first_byte_deadline_s``. Hermetic — no network, no .env.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from loguru import logger as loguru_logger

from repoach.llm_proxy.api.app import create_app
from repoach.llm_proxy.config.settings import get_settings
from repoach.llm_proxy.providers.base import BaseProvider, ProviderConfig
from repoach.llm_proxy.providers.registry import ProviderRegistry
from repoach.llm_proxy.routing import reset_breaker


class _SilentProvider(BaseProvider):
    """Provider whose stream accepts the request but never yields a chunk."""

    SUPPORTS_NATIVE_TOOLS: bool = True

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)

    async def cleanup(self) -> None:
        return None

    async def stream_response(
        self,
        request: Any,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
    ) -> AsyncIterator[str]:
        await asyncio.sleep(30.0)
        yield "event: message_stop\ndata: {}\n\n"


class _HealthyProvider(BaseProvider):
    """Provider whose stream always yields real text content."""

    SUPPORTS_NATIVE_TOOLS: bool = True

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)

    async def cleanup(self) -> None:
        return None

    async def stream_response(
        self,
        request: Any,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
    ) -> AsyncIterator[str]:
        yield (
            "event: message_start\n"
            'data: {"type":"message_start","message":{"id":"x","type":"message",'
            '"role":"assistant","content":[],"model":"test","stop_reason":null,'
            '"usage":{"input_tokens":10,"output_tokens":0}}}\n\n'
        )
        yield (
            "event: content_block_start\n"
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"text","text":""}}\n\n'
        )
        yield (
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"Hello world"}}\n\n'
        )
        yield 'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
        yield (
            "event: message_delta\n"
            'data: {"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n\n'
        )
        yield 'event: message_stop\ndata: {"type":"message_stop"}\n\n'


@pytest.fixture()
def captured_loguru() -> list[Any]:
    """Capture loguru records for the duration of a test.

    The proxy uses ``loguru`` rather than structlog, so a small sink
    buffers structured records for assertion — mirrors the fixture in
    ``tests/unit/test_proxy_failover_events.py``.
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


def _find(records: list[Any], event: str) -> Any:
    for r in records:
        if r["message"] == event:
            return r
    raise AssertionError(f"event {event!r} not in {[r['message'] for r in records]}")


def test_silent_hop_fails_over_at_first_byte_deadline_not_full_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    captured_loguru: list[Any],
) -> None:
    """A silent first hop fails over to the healthy second hop well under ``http_read_timeout``.

    The settings singleton has ``first_byte_deadline_s`` shrunk to
    0.2s so the silent hop's 30s sleep aborts almost immediately
    instead of running out the (much larger) blanket read timeout.
    """
    reset_breaker()

    monkeypatch.setenv("REPOACH_ANTHROPIC_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("REPOACH_PROXY_DEFAULT_MODEL", "nvidia_nim/good-model")
    monkeypatch.setenv("MODEL_SONNET", "nvidia_nim/silent-model,kimi/healthy-model")
    get_settings.cache_clear()
    request.addfinalizer(get_settings.cache_clear)
    monkeypatch.setattr(get_settings(), "anthropic_auth_token", "test-token")
    monkeypatch.setattr(get_settings(), "first_byte_deadline_s", 0.2)

    app = create_app()

    silent_provider = _SilentProvider(ProviderConfig(api_key="x"))
    healthy_provider = _HealthyProvider(ProviderConfig(api_key="x"))
    registry = ProviderRegistry({"nvidia_nim": silent_provider, "kimi": healthy_provider})

    headers = {"x-api-key": "test-token"}
    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "ping"}],
    }

    with TestClient(app) as client:
        app.state.provider_registry = registry
        started = time.monotonic()
        resp = client.post("/v1/messages", json=body, headers=headers)
        elapsed = time.monotonic() - started

    assert resp.status_code == 200, resp.text
    assert "Hello world" in resp.text
    assert elapsed < 10.0, (
        f"expected failover well under http_read_timeout (120s default), took {elapsed}s"
    )

    fired = _find(captured_loguru, "proxy_chain_failover_fired")
    assert fired["extra"]["primary_reason"] == "first_byte_timeout"
