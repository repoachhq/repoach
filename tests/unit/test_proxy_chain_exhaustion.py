"""Unit/integration tests for SP-STREAM-EXHAUST-ERROR.

Drives ``/v1/messages`` end-to-end through ``FastAPI TestClient`` (a
real ASGI request/response cycle, so headers really are committed
before the body iterator ever runs) with truthful boundary-fake
providers (real ``BaseProvider`` implementations raising real
exceptions — the same seam ``tests/integration/
test_provider_scope_and_credits_gate.py`` uses; no ``httpx.MockTransport``
seam exists on the concrete provider clients' AsyncOpenAI-wrapped HTTP
layer, and monkeypatching that internal is out of scope for a
truthful fake).

Two chain-exhaustion sub-cases:

1. Every candidate is dispatched and fails: headers are already
   committed 200 by the time the walk gives up, so the client can only
   ever see the failure via a terminal SSE ``error`` event inside the
   200 body (G1).
2. Every candidate is already breaker-tripped before the walk starts:
   ``create_message`` can detect this synchronously and return a real
   HTTP 502 before the ``StreamingResponse`` — and its 200 headers —
   ever exist (G2).
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from repoach.llm_proxy.api.app import create_app
from repoach.llm_proxy.api.dependencies import get_settings
from repoach.llm_proxy.api.routes import get_proxy_service
from repoach.llm_proxy.api.services import ClaudeProxyService
from repoach.llm_proxy.config import settings as settings_module
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.base import BaseProvider, ProviderConfig
from repoach.llm_proxy.routing import get_breaker, reset_breaker
from repoach.llm_proxy.routing.refs import ModelRef


@pytest.fixture(autouse=True)
def _reset_breaker_state() -> None:
    reset_breaker()
    yield
    reset_breaker()


class _AlwaysFailingProvider(BaseProvider):
    """Truthful boundary fake: every ``stream_response`` call raises."""

    SUPPORTS_NATIVE_TOOLS: bool = True

    def __init__(self, config: ProviderConfig, *, error_message: str) -> None:
        super().__init__(config)
        self._error_message = error_message
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
        raise RuntimeError(self._error_message)
        yield ""


def _request_body() -> dict[str, Any]:
    return {
        "model": "claude-sonnet-4-6",
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "ping"}],
    }


def _wire_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    chain_env: str,
    providers: dict[str, BaseProvider],
) -> TestClient:
    monkeypatch.setenv("MODEL", "nvidia_nim/z-ai/glm4.7")
    monkeypatch.setenv("MODEL_SONNET", chain_env)
    monkeypatch.setattr(settings_module, "_configured_env_files", lambda _cfg: ())
    settings = Settings(_env_file=None)
    service = ClaudeProxyService(
        settings=settings,
        provider_getter=lambda provider_id: providers[provider_id],
        token_counter=lambda *args, **kwargs: 0,
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_proxy_service] = lambda: service
    return TestClient(app)


def _parse_sse_events(body: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: ") :]))
    return events


def test_exhaustion_emits_terminal_sse_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1/AC2: all hops fail after streaming has started (200 committed).

    Both candidates get dispatched and raise; the caller must receive
    a well-formed terminal SSE ``error`` event carrying the explicit
    ``chain_exhausted`` type — never a bare/silent stream end.
    """
    providers: dict[str, BaseProvider] = {
        "nvidia_nim": _AlwaysFailingProvider(ProviderConfig(api_key="x"), error_message="nim down"),
        "kimi": _AlwaysFailingProvider(ProviderConfig(api_key="x"), error_message="kimi down"),
    }
    client = _wire_client(
        monkeypatch,
        chain_env="nvidia_nim/meta/llama-3.3-70b-instruct,kimi/kimi-k2.6",
        providers=providers,
    )

    with client:
        response = client.post("/v1/messages", json=_request_body())

    assert response.status_code == 200, (
        "headers are committed 200 the instant Starlette starts iterating the "
        "body, before the chain walk can know it will exhaust (NG2)"
    )
    assert providers["nvidia_nim"].call_count == 1
    assert providers["kimi"].call_count == 1

    events = _parse_sse_events(response.text)
    assert events, "the body must not end bare/empty without any SSE event at all"

    error_events = [e for e in events if e.get("type") == "error"]
    assert error_events, (
        f"expected a terminal SSE error event, got only: {[e.get('type') for e in events]}"
    )
    terminal_error = error_events[-1]["error"]
    assert terminal_error["type"] == "chain_exhausted"
    assert isinstance(terminal_error["message"], str) and terminal_error["message"]


def test_preflight_exhaustion_returns_502(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2 pre-flight half: every candidate already breaker-tripped.

    The sole configured candidate is tripped BEFORE the request is
    ever issued, so ``create_message`` can know the chain is exhausted
    synchronously and return a real HTTP 502 before the
    ``StreamingResponse`` (and its 200 headers) ever exist — the
    provider must never even be dispatched.
    """
    ref = ModelRef.parse("nvidia_nim/only-model")
    get_breaker().trip(ref, now=time.monotonic(), ttl_s=120.0, reason="test_preflight_tripped")

    providers: dict[str, BaseProvider] = {
        "nvidia_nim": _AlwaysFailingProvider(
            ProviderConfig(api_key="x"), error_message="must never be dispatched"
        ),
    }
    client = _wire_client(
        monkeypatch,
        chain_env="nvidia_nim/only-model",
        providers=providers,
    )

    with client:
        response = client.post("/v1/messages", json=_request_body())

    assert response.status_code == 502
    assert providers["nvidia_nim"].call_count == 0, (
        "a pre-flight-exhausted chain must return 502 without dispatching a single candidate"
    )
    assert "breaker-tripped" in json.dumps(response.json())
