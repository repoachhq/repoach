"""Integration test for SP-BREAKER-SLOW-STRIKE step 7.

End-to-end: a chronically slow-but-served completion accrues k-of-n slow
strikes and, once enforced, trips the ref with reason ``slow_completion``
so it is filtered from the next chain resolve and surfaced on
``GET /health``. In shadow mode (the default) the same traffic only logs
``breaker_slow_strike_shadow`` and never trips. Hermetic — no network, no
``.env``, following the truthful-boundary-fake pattern of
``test_proxy_dead_hop_quarantine.py``: stub ``BaseProvider`` subclasses
stand in for the upstream, and a slow stub actually sleeps past the
(shrunk) ``breaker_slow_latency_gate_s`` rather than faking a latency
value.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from loguru import logger

from repoach.llm_proxy.api.app import create_app
from repoach.llm_proxy.config.settings import get_settings
from repoach.llm_proxy.providers.base import BaseProvider, ProviderConfig
from repoach.llm_proxy.providers.registry import ProviderRegistry
from repoach.llm_proxy.routing import get_breaker, reset_breaker
from repoach.llm_proxy.routing.refs import ModelRef


class _SlowThinProvider(BaseProvider):
    """Provider whose stream actually sleeps past a latency gate before
    yielding a thin (low tokens-per-second) but real completion.

    Sleeping for real — rather than faking a latency number — is the
    truthful-boundary-fake contract this suite follows: the test shrinks
    ``breaker_slow_latency_gate_s`` so the sleep stays short while the
    dispatcher still measures a genuine wall-clock latency past the gate.
    """

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
            '"delta":{"type":"text_delta","text":"Slow reply"}}\n\n'
        )
        yield 'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
        yield (
            "event: message_delta\n"
            'data: {"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n'
        )
        yield 'event: message_stop\ndata: {"type":"message_stop"}\n\n'


class _HealthyProvider(BaseProvider):
    """Provider whose stream always yields a fast, real completion."""

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


_BODY = {
    "model": "claude-sonnet-4-20250514",
    "max_tokens": 32,
    "messages": [{"role": "user", "content": "ping"}],
}
_HEADERS = {"x-api-key": "test-token"}


def _shrink_slow_gates(
    monkeypatch: pytest.MonkeyPatch,
    *,
    shadow: bool,
    k: int = 2,
    n: int = 5,
    ttl_s: float = 50.0,
) -> None:
    """Apply the shrunk slow-breaker gates shared by both scenarios.

    ``breaker_slow_latency_gate_s`` is shrunk to 20 ms so the stub
    provider's real sleep can cross it quickly, and
    ``breaker_slow_tps_floor`` is raised well above what a one-token
    completion could ever achieve at that latency, so the stub's
    single-token reply always classifies as thin.
    """
    monkeypatch.setattr(get_settings(), "breaker_slow_latency_gate_s", 0.02)
    monkeypatch.setattr(get_settings(), "breaker_slow_tps_floor", 1000.0)
    monkeypatch.setattr(get_settings(), "breaker_slow_k", k)
    monkeypatch.setattr(get_settings(), "breaker_slow_n", n)
    monkeypatch.setattr(get_settings(), "breaker_slow_ttl_s", ttl_s)
    monkeypatch.setattr(get_settings(), "breaker_slow_shadow", shadow)


def test_slow_breaker_integration_shadow_mode(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """With ``breaker_slow_shadow=true`` (the default) k slow-but-served
    completions are recorded and logged as ``breaker_slow_strike_shadow``
    once the k-of-n window is reached, but the ref is never tripped: it
    stays absent from ``GET /health`` and keeps serving traffic.
    """
    reset_breaker()

    monkeypatch.setenv("REPOACH_ANTHROPIC_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("REPOACH_PROXY_DEFAULT_MODEL", "nvidia_nim/slow-model")
    monkeypatch.setenv("MODEL_SONNET", "nvidia_nim/slow-model")
    get_settings.cache_clear()
    request.addfinalizer(get_settings.cache_clear)
    monkeypatch.setattr(get_settings(), "anthropic_auth_token", "test-token")
    _shrink_slow_gates(monkeypatch, shadow=True)

    app = create_app()
    slow_provider = _SlowThinProvider(ProviderConfig(api_key="x"), sleep_s=0.05)
    registry = ProviderRegistry({"nvidia_nim": slow_provider})

    captured: list[str] = []
    sink_id = logger.add(lambda message: captured.append(str(message)), level="INFO")
    request.addfinalizer(lambda: logger.remove(sink_id))

    slow_ref = ModelRef.parse("nvidia_nim/slow-model")

    with TestClient(app) as client:
        app.state.provider_registry = registry
        for i in range(2):
            resp = client.post("/v1/messages", json=_BODY, headers=_HEADERS)
            assert resp.status_code == 200, f"request {i + 1} failed: {resp.text}"

        breaker = get_breaker()
        now = time.monotonic()
        assert not breaker.is_down(slow_ref, now), (
            "shadow mode must never trip the ref regardless of strike count"
        )

        health_resp = client.get("/health")
        assert health_resp.status_code == 200
        breaker_entries = health_resp.json()["breaker"]
        assert not any(e["ref"] == "nvidia_nim/slow-model" for e in breaker_entries), (
            "shadow mode must not surface a breaker entry on /health"
        )

        assert any("breaker_slow_strike_shadow" in line for line in captured), (
            "shadow mode should log breaker_slow_strike_shadow once k is reached"
        )

        resp3 = client.post("/v1/messages", json=_BODY, headers=_HEADERS)
        assert resp3.status_code == 200
        assert "Slow reply" in resp3.text


def test_slow_breaker_integration_enforcing_mode(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """With ``breaker_slow_shadow=false`` and a shrunk k-of-n window, the
    k-th slow-but-served completion trips the ref with reason
    ``slow_completion``: it appears on ``GET /health`` with that reason
    and a TTL bounded by ``breaker_slow_ttl_s``, and the next chain
    resolve skips it in favor of the healthy fallback hop.
    """
    reset_breaker()

    monkeypatch.setenv("REPOACH_ANTHROPIC_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("REPOACH_PROXY_DEFAULT_MODEL", "nvidia_nim/slow-model")
    monkeypatch.setenv("MODEL_SONNET", "nvidia_nim/slow-model,kimi/healthy-model")
    get_settings.cache_clear()
    request.addfinalizer(get_settings.cache_clear)
    monkeypatch.setattr(get_settings(), "anthropic_auth_token", "test-token")
    _shrink_slow_gates(monkeypatch, shadow=False, k=2, n=5, ttl_s=50.0)

    app = create_app()
    slow_provider = _SlowThinProvider(ProviderConfig(api_key="x"), sleep_s=0.05)
    healthy_provider = _HealthyProvider(ProviderConfig(api_key="x"))
    registry = ProviderRegistry({"nvidia_nim": slow_provider, "kimi": healthy_provider})

    slow_ref = ModelRef.parse("nvidia_nim/slow-model")

    with TestClient(app) as client:
        app.state.provider_registry = registry
        for i in range(2):
            resp = client.post("/v1/messages", json=_BODY, headers=_HEADERS)
            assert resp.status_code == 200, f"request {i + 1} failed: {resp.text}"
            assert "Slow reply" in resp.text

        breaker = get_breaker()
        now = time.monotonic()
        assert breaker.is_down(slow_ref, now), (
            "the slow ref should be tripped after k slow completions"
        )

        health_resp = client.get("/health")
        assert health_resp.status_code == 200
        breaker_entries = health_resp.json()["breaker"]
        slow_entry = next(
            (e for e in breaker_entries if e["ref"] == "nvidia_nim/slow-model"),
            None,
        )
        assert slow_entry is not None, "slow-tripped ref missing from /health breaker array"
        assert slow_entry["reason"] == "slow_completion"
        assert slow_entry["ttl_remaining_s"] <= 50.0, (
            "a slow_completion trip must use the dedicated slow TTL, never the hard-quarantine TTL"
        )

        resp3 = client.post("/v1/messages", json=_BODY, headers=_HEADERS)
        assert resp3.status_code == 200
        assert "Hello world" in resp3.text, (
            "the tripped slow ref must be filtered from the next resolve so the "
            "healthy fallback serves the request"
        )
