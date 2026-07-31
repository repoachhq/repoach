"""Integration test for SP-BREAKER-PROVIDER-SCOPE step 2.

End-to-end: a 402 on one open_router hop propagates to every open_router
ref in the chain via provider-scoped breaker trip, the sibling is skipped
mid-iteration, the request completes on the fallback, and GET /health
lists both open_router refs with the propagated reason. Hermetic — no
network, no .env. Drives ClaudeProxyService directly (the fake-registry
seam used by test_proxy_chain_failover.py) for the dispatch portion so
the assertion surface is the dispatcher itself, then reuses the same
process-level breaker singleton for the GET /health check via
TestClient (the test_proxy_dead_hop_quarantine.py style).
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from repoach.llm_proxy.api.app import create_app
from repoach.llm_proxy.api.dependencies import get_settings as get_settings_dependency
from repoach.llm_proxy.api.models.anthropic import MessagesRequest
from repoach.llm_proxy.api.services import ClaudeProxyService
from repoach.llm_proxy.config import settings as settings_module
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.base import BaseProvider, ProviderConfig
from repoach.llm_proxy.providers.exceptions import APIError
from repoach.llm_proxy.routing import get_breaker, reset_breaker
from repoach.llm_proxy.routing.refs import ModelRef


class _Failing402Provider(BaseProvider):
    """Provider whose stream_response always raises a 402 API error."""

    SUPPORTS_NATIVE_TOOLS: bool = True

    def __init__(self, config: ProviderConfig, *, call_log: list[str]) -> None:
        super().__init__(config)
        self._call_log = call_log

    async def cleanup(self) -> None:
        return None

    async def stream_response(
        self,
        request: Any,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
    ) -> AsyncIterator[str]:
        self._call_log.append(f"open_router/{request.model}")
        raise APIError("Insufficient credits", status_code=402)
        yield ""


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
            '"delta":{"type":"text_delta","text":"Hello from fallback"}}\n\n'
        )
        yield 'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
        yield (
            "event: message_delta\n"
            'data: {"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n\n'
        )
        yield 'event: message_stop\ndata: {"type":"message_stop"}\n\n'


async def _drain(response: Any) -> str:
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, str) else chunk.decode())
    return "".join(chunks)


@pytest.mark.anyio
async def test_one_402_skips_sibling_refs_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 402 on the first open_router hop benches all open_router refs in
    the chain; the sibling is skipped mid-iteration; the request completes
    on the healthy fallback; /health lists all open_router refs with a
    provider_402_propagated reason.
    """
    reset_breaker()
    monkeypatch.setenv("REPOACH_BREAKER_ENABLED", "true")
    monkeypatch.setenv("REPOACH_PROXY_DEFAULT_MODEL", "kimi/healthy-model")
    monkeypatch.setenv(
        "MODEL_SONNET",
        "open_router/failing-model,open_router/second-model,kimi/healthy-model",
    )
    settings = Settings(_env_file=None)

    call_log: list[str] = []
    failing_provider = _Failing402Provider(ProviderConfig(api_key="x"), call_log=call_log)
    healthy_provider = _HealthyProvider(ProviderConfig(api_key="x"))
    providers: dict[str, BaseProvider] = {
        "open_router": failing_provider,
        "kimi": healthy_provider,
    }

    service = ClaudeProxyService(
        settings=settings,
        provider_getter=lambda provider_id: providers[provider_id],
    )

    request_data = MessagesRequest(
        model="claude-sonnet-4-20250514",
        max_tokens=32,
        messages=[{"role": "user", "content": "ping"}],
    )

    response = service.create_message(request_data)
    body = await _drain(response)

    assert "Hello from fallback" in body
    assert call_log == ["open_router/failing-model"], (
        f"only the first open_router ref should have been called; got {call_log}"
    )

    breaker = get_breaker()
    failing_ref = ModelRef.parse("open_router/failing-model")
    second_ref = ModelRef.parse("open_router/second-model")
    now = time.monotonic()

    assert breaker.is_down(failing_ref, now), "first open_router ref must be down"
    assert breaker.is_down(second_ref, now), "second open_router ref must be down via propagation"
    assert breaker._down_reason.get(failing_ref) == "provider_402_propagated"
    assert breaker._down_reason.get(second_ref) == "provider_402_propagated"

    monkeypatch.setattr(settings_module, "_configured_env_files", lambda _cfg: ())
    app = create_app()
    app.dependency_overrides[get_settings_dependency] = lambda: Settings(_env_file=None)
    with TestClient(app) as client:
        health_resp = client.get("/health")
        assert health_resp.status_code == 200
        breaker_entries = health_resp.json()["breaker"]

    open_router_entries = [e for e in breaker_entries if e["ref"].startswith("open_router/")]
    assert len(open_router_entries) == 2, (
        f"expected 2 open_router breaker entries, got {json.dumps(breaker_entries)}"
    )
    for entry in open_router_entries:
        assert entry["reason"] == "provider_402_propagated"
        assert entry["consecutive_failures"] == 1
