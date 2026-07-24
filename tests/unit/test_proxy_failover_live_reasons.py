"""Unit tests for SP-BREAKER-LIVE-REASONS.

Audit 2026-07-13 H10: on live traffic every provider failure was
classified as ``empty_completion`` and tripped with the short transient
TTL, because the HTTP transports caught every upstream exception
inside the streaming generator and degraded it to an in-band SSE error
instead of raising or otherwise surfacing the real status — so a 402
dead hop was retried every 120s instead of quarantined for 6h
(SP-CHAIN-DEAD-HOP-QUARANTINE never fired).

These tests drive the REAL failover path: an :class:`OpenRouterProvider`
backed by an ``httpx.MockTransport`` (a truthful boundary fake — only
the HTTP transport is faked, no Ferova code is monkeypatched) returns a
genuine 402/401/429 HTTP response; ``ClaudeProxyService._stream_with_failover``
walks a one-candidate chain and the real process-level breaker records
the outcome.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from repoach.llm_proxy.api.model_router import ResolvedModel
from repoach.llm_proxy.api.models.anthropic import Message, MessagesRequest
from repoach.llm_proxy.api.services import ClaudeProxyService, _classify_failover_reason
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.base import ProviderConfig
from repoach.llm_proxy.providers.exceptions import UpstreamStatusError
from repoach.llm_proxy.providers.open_router.client import OpenRouterProvider
from repoach.llm_proxy.routing import get_breaker, reset_breaker
from repoach.llm_proxy.routing.refs import ModelRef


def _ref(spec: str) -> ModelRef:
    return ModelRef.parse(spec)


@pytest.fixture(autouse=True)
def _hermetic_breaker() -> None:
    reset_breaker()


def _mock_response_transport(status_code: int) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"error": {"message": f"upstream returned {status_code}"}},
            request=request,
        )

    return httpx.MockTransport(handler)


def _open_router_provider(status_code: int) -> OpenRouterProvider:
    provider = OpenRouterProvider(ProviderConfig(api_key="test-key"))
    provider._client = httpx.AsyncClient(
        base_url="https://mock.example",
        transport=_mock_response_transport(status_code),
    )
    return provider


def _candidate(ref: str) -> ResolvedModel:
    provider_id, provider_model = ref.split("/", 1)
    return ResolvedModel(
        original_model="claude-sonnet-4",
        provider_id=provider_id,
        provider_model=provider_model,
        provider_model_ref=ref,
    )


def _request() -> MessagesRequest:
    return MessagesRequest(
        model="claude-sonnet-4",
        max_tokens=64,
        messages=[Message(role="user", content="ping")],
    )


def _service(monkeypatch: pytest.MonkeyPatch, provider: OpenRouterProvider) -> ClaudeProxyService:
    monkeypatch.setenv("REPOACH_BREAKER_ENABLED", "true")
    monkeypatch.setenv("REPOACH_BREAKER_TTL_S", "120")
    monkeypatch.setenv("REPOACH_BREAKER_TTL_QUARANTINE_S", "21600")
    settings = Settings(_env_file=None)
    return ClaudeProxyService(
        settings=settings,
        provider_getter=lambda _provider_id: provider,
        token_counter=lambda *_args, **_kwargs: 0,
    )


async def _drain(service: ClaudeProxyService, chain: list[ResolvedModel]) -> list[str]:
    chunks: list[str] = []
    async for chunk in service._stream_with_failover(_request(), chain, input_tokens=5):
        chunks.append(chunk)
    return chunks


def test_upstream_402_quarantines(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real HTTP 402 from the only chain candidate trips the breaker
    with reason ``provider_402`` at the quarantine TTL — not
    ``empty_completion`` at the short transient TTL."""
    provider = _open_router_provider(402)
    service = _service(monkeypatch, provider)
    ref = _ref("open_router/anthropic/claude-sonnet-4")
    chain = [_candidate("open_router/anthropic/claude-sonnet-4")]

    base = time.monotonic()
    asyncio.run(_drain(service, chain))

    assert get_breaker().is_down(ref, base + 130.0)
    assert get_breaker().is_down(ref, base + 120.0 + 5.0), (
        "a provider_402 dead hop must survive the short transient TTL"
        " (empty_completion's TTL) — it must be quarantined instead"
    )
    assert get_breaker().is_down(ref, base + 21_600.0 - 5.0)
    assert not get_breaker().is_down(ref, base + 21_600.0 + 5.0)
    assert get_breaker()._down_reason[ref].startswith("provider_402"), (
        "the recorded reason must carry the real HTTP 402, not empty_completion"
        f"; got {get_breaker()._down_reason[ref]!r}"
    )


def test_upstream_401_and_429_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real 401 quarantines with reason auth_failed (account-fault
    propagation appends its own ``_propagated`` suffix, unrelated to
    this spec — SP-BREAKER-PROVIDER-SCOPE); a real 429 keeps the
    transient TTL with reason rate_limited exactly (429 is not an
    account-fault reason, so no propagation suffix applies)."""
    provider_401 = _open_router_provider(401)
    service_401 = _service(monkeypatch, provider_401)
    ref_401 = _ref("open_router/anthropic/claude-sonnet-4")
    chain_401 = [_candidate("open_router/anthropic/claude-sonnet-4")]

    base_401 = time.monotonic()
    asyncio.run(_drain(service_401, chain_401))

    assert get_breaker()._down_reason[ref_401].startswith("auth_failed")
    assert get_breaker().is_down(ref_401, base_401 + 21_600.0 - 5.0)
    assert not get_breaker().is_down(ref_401, base_401 + 21_600.0 + 5.0)

    reset_breaker()

    provider_429 = _open_router_provider(429)
    service_429 = _service(monkeypatch, provider_429)
    ref_429 = _ref("open_router/qwen/qwen3.7-max")
    chain_429 = [_candidate("open_router/qwen/qwen3.7-max")]

    base_429 = time.monotonic()
    asyncio.run(_drain(service_429, chain_429))

    assert get_breaker()._down_reason[ref_429] == "rate_limited"
    assert get_breaker().is_down(ref_429, base_429 + 60.0)
    assert not get_breaker().is_down(ref_429, base_429 + 120.0 + 5.0), (
        "rate_limited is transient, not a quarantine reason"
    )


def test_classify_failover_reason_recovers_upstream_status_vocabulary() -> None:
    """_classify_failover_reason recovers the exact spec vocabulary from
    an UpstreamStatusError rebuilt off a bare recovered status code."""
    assert _classify_failover_reason(UpstreamStatusError(402)) == "provider_402"
    assert _classify_failover_reason(UpstreamStatusError(401)) == "auth_failed"
    assert _classify_failover_reason(UpstreamStatusError(429)) == "rate_limited"
    assert _classify_failover_reason(UpstreamStatusError(500)) == "provider_5xx"
