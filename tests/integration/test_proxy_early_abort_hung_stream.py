"""Integration test for SP-PROXY-EARLY-ABORT-ERROR-FRAME.

End-to-end: a chain whose first hop emits the documented disguised
connection-error text and then hangs for several seconds before ever
reaching its terminal ``message_delta``/``message_stop`` fails over to
the second, healthy hop almost immediately — the drain aborts on the
disguised-error text itself rather than waiting out the hang.
Hermetic — no network, no .env.
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


class _HungDisguisedErrorProvider(BaseProvider):
    """Emits the documented disguised connection-error text, then hangs.

    ``closed`` is set from a ``finally`` wrapping the whole generator
    body so it reads ``True`` regardless of which yield the abandoned
    generator was suspended at when ``aclose()`` fires.
    """

    SUPPORTS_NATIVE_TOOLS: bool = True

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self.closed = False

    async def cleanup(self) -> None:
        return None

    async def stream_response(
        self,
        request: Any,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
    ) -> AsyncIterator[str]:
        try:
            yield "event: message_start\ndata: {}\n\n"
            yield (
                "event: content_block_delta\n"
                'data: {"type":"content_block_delta","index":0,'
                '"delta":{"type":"text_delta",'
                '"text":"Connection error. (request_id=req_def232f1cfca)"}}\n\n'
            )
            await asyncio.sleep(4.0)
            yield (
                "event: message_delta\n"
                'data: {"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":0}}\n\n'
            )
            yield "event: message_stop\ndata: {}\n\n"
        finally:
            self.closed = True


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


def test_hung_disguised_error_candidate_fails_over_without_waiting_for_the_hang(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """The disguised-error text aborts the drain immediately, so failover
    to the healthy second hop takes well under 2 seconds instead of
    waiting out the first hop's 4-second simulated hang.

    ``budget_retry_enabled`` is forced ``False`` on the resolved
    ``Settings`` to keep the timing story isolated to this spec's
    change (the disguised-error path is not budget-starved regardless,
    per G5, but the setting removes any ambiguity). The breaker/effort-map
    probe-seed steps are disabled too — they read the real, non-hermetic
    ``data/repoach.db`` at startup and can pre-trip an unrelated tier
    head from a developer machine's live probe history, which would
    otherwise make this test flaky outside a clean CI checkout.
    """
    reset_breaker()

    monkeypatch.setenv("REPOACH_ANTHROPIC_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("REPOACH_PROXY_DEFAULT_MODEL", "nvidia_nim/good-model")
    monkeypatch.setenv("MODEL_SONNET", "nvidia_nim/hung-model,kimi/healthy-model")
    get_settings.cache_clear()
    request.addfinalizer(get_settings.cache_clear)
    monkeypatch.setattr(get_settings(), "anthropic_auth_token", "test-token")
    monkeypatch.setattr(get_settings(), "budget_retry_enabled", False)
    monkeypatch.setattr(get_settings(), "breaker_probe_seed_enabled", False)
    monkeypatch.setattr(get_settings(), "effort_map_seed_enabled", False)

    app = create_app()

    hung_provider = _HungDisguisedErrorProvider(ProviderConfig(api_key="x"))
    healthy_provider = _HealthyProvider(ProviderConfig(api_key="x"))
    registry = ProviderRegistry({"nvidia_nim": hung_provider, "kimi": healthy_provider})

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
    assert elapsed < 2.0, f"expected failover well under the 4s simulated hang, took {elapsed}s"
    assert hung_provider.closed is True
