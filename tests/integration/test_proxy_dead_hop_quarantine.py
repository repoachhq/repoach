"""Integration test for SP-CHAIN-DEAD-HOP-QUARANTINE step 4.

End-to-end: a dead chain hop is quarantined after three empty-completion
streams and surfaced on GET /health.  Hermetic — no network, no .env.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ferova.llm_proxy.api.app import create_app
from ferova.llm_proxy.providers.base import BaseProvider, ProviderConfig
from ferova.llm_proxy.providers.registry import ProviderRegistry
from ferova.llm_proxy.routing import get_breaker, reset_breaker
from ferova.llm_proxy.routing.refs import ModelRef


class _EmptyProvider(BaseProvider):
    """Provider whose stream always yields an empty completion (zero output tokens)."""

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
        yield "event: message_start\ndata: {}\n\n"
        yield (
            "event: message_delta\n"
            'data: {"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":0}}\n\n'
        )
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
            '"content_block":{"type":"text","text":"hello"}}\n\n'
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


def test_dead_hop_quarantined_and_reported_on_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three requests against a dead-first chain quarantine the dead hop
    with a TTL > breaker_ttl_s, and /health lists it with >= 3 consecutive
    failures while the healthy hop keeps serving."""
    reset_breaker()

    monkeypatch.setenv("FEROVA_ANTHROPIC_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("FEROVA_PROXY_DEFAULT_MODEL", "nvidia_nim/good-model")
    monkeypatch.setenv(
        "MODEL_SONNET",
        "nvidia_nim/dead-model,kimi/healthy-model",
    )

    app = create_app()

    dead_provider = _EmptyProvider(ProviderConfig(api_key="x"))
    healthy_provider = _HealthyProvider(ProviderConfig(api_key="x"))
    registry = ProviderRegistry({"nvidia_nim": dead_provider, "kimi": healthy_provider})
    app.state.provider_registry = registry

    headers = {"x-api-key": "test-token"}
    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "ping"}],
    }

    with TestClient(app) as client:
        for i in range(3):
            resp = client.post("/v1/messages", json=body, headers=headers)
            assert resp.status_code == 200, f"request {i + 1} failed: {resp.text}"

        breaker = get_breaker()
        dead_ref = ModelRef.parse("nvidia_nim/dead-model")
        now = time.monotonic()
        assert breaker.is_down(dead_ref, now), "dead hop should be down after three failures"

        health_resp = client.get("/health")
        assert health_resp.status_code == 200
        health_body = health_resp.json()
        breaker_entries = health_body["breaker"]

        assert len(breaker_entries) >= 1
        dead_entry = next(
            (e for e in breaker_entries if e["ref"] == "nvidia_nim/dead-model"),
            None,
        )
        assert dead_entry is not None, "dead hop missing from /health breaker array"
        assert dead_entry["consecutive_failures"] >= 3
        assert dead_entry["reason"] == "empty_completion"
        assert dead_entry["ttl_remaining_s"] > 120.0, (
            "quarantine TTL should exceed transient breaker_ttl_s (120 s)"
        )

        resp4 = client.post("/v1/messages", json=body, headers=headers)
        assert resp4.status_code == 200
        assert "Hello world" in resp4.text
