"""Integration test for SP-CHAIN-REQUEST-BUDGET.

End-to-end: a slow native hop is preempted by the total dispatch
budget so the ``claude_code`` backstop still serves within its own
floor, well under what waiting out the native hop's full sleep would
have cost. Hermetic — no network, no .env.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from repoach.llm_proxy.api.app import create_app
from repoach.llm_proxy.config.settings import get_settings
from repoach.llm_proxy.providers.base import BaseProvider, ProviderConfig
from repoach.llm_proxy.providers.registry import ProviderRegistry
from repoach.llm_proxy.routing import reset_breaker


class _SlowNativeProvider(BaseProvider):
    """Provider whose stream sleeps far longer than the configured budget."""

    SUPPORTS_NATIVE_TOOLS: bool = True

    def __init__(self, config: ProviderConfig, *, sleep_s: float) -> None:
        super().__init__(config)
        self._sleep_s = sleep_s

    async def cleanup(self) -> None:
        return None

    async def stream_response(
        self,
        request: Any,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
    ) -> AsyncIterator[str]:
        await asyncio.sleep(self._sleep_s)
        yield "event: message_stop\ndata: {}\n\n"


class _ClaudeCodeBackstopProvider(BaseProvider):
    """Provider whose stream sleeps briefly then yields real content."""

    SUPPORTS_NATIVE_TOOLS: bool = True

    def __init__(self, config: ProviderConfig, *, sleep_s: float) -> None:
        super().__init__(config)
        self._sleep_s = sleep_s

    async def cleanup(self) -> None:
        return None

    async def stream_response(
        self,
        request: Any,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
    ) -> AsyncIterator[str]:
        await asyncio.sleep(self._sleep_s)
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
            '"delta":{"type":"text_delta","text":"claude_code backstop response"}}\n\n'
        )
        yield 'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
        yield (
            "event: message_delta\n"
            'data: {"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n\n'
        )
        yield 'event: message_stop\ndata: {"type":"message_stop"}\n\n'


def test_slow_native_hop_is_preempted_so_claude_code_backstop_still_serves_within_its_floor(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """A slow ``nvidia_nim`` hop is preempted by ``dispatch_total_budget_s``;
    ``claude_code`` still serves real content within its own
    ``claude_code_subprocess_timeout`` floor, well under the native
    hop's full sleep duration.
    """
    reset_breaker()

    monkeypatch.setenv("REPOACH_ANTHROPIC_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("REPOACH_PROXY_DEFAULT_MODEL", "nvidia_nim/good-model")
    monkeypatch.setenv("MODEL_SONNET", "nvidia_nim/slow-model,claude_code/sonnet")
    get_settings.cache_clear()
    request.addfinalizer(get_settings.cache_clear)
    monkeypatch.setattr(get_settings(), "anthropic_auth_token", "test-token")
    monkeypatch.setattr(get_settings(), "dispatch_total_budget_s", 0.2)
    monkeypatch.setattr(get_settings(), "claude_code_subprocess_timeout", 0.3)

    app = create_app()

    slow_sleep_s = 60.0
    slow_provider = _SlowNativeProvider(ProviderConfig(api_key="x"), sleep_s=slow_sleep_s)
    backstop_provider = _ClaudeCodeBackstopProvider(ProviderConfig(api_key="x"), sleep_s=0.05)
    registry = ProviderRegistry({"nvidia_nim": slow_provider, "claude_code": backstop_provider})

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
    assert "claude_code backstop response" in resp.text
    assert elapsed < slow_sleep_s * 0.25, (
        f"expected total elapsed well under the native hop's {slow_sleep_s}s sleep, took {elapsed}s"
    )
