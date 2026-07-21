"""Unit tests for the SP-BREAKER-PROVIDER-SCOPE proactive credits gate.

Drives :func:`repoach.llm_proxy.api.model_router.compute_credits_gate_skip_models`
with an ``httpx.AsyncClient`` backed by ``httpx.MockTransport`` — the same
boundary-fake style as ``tests/unit/test_credits.py`` — and resets the
module-level credits cache before each test so no state leaks across
tests.
"""

from __future__ import annotations

import time

import httpx
import pytest

from repoach.health.credits import reset_credits_cache
from repoach.llm_proxy.api.model_router import compute_credits_gate_skip_models
from repoach.llm_proxy.api.services import ClaudeProxyService
from repoach.llm_proxy.config.settings import Settings

_OPEN_ROUTER_REFS = frozenset({"open_router/qwen/qwen3.7-max", "open_router/x-ai/grok-4"})


def _make_client(
    *,
    status_code: int = 200,
    json_body: object = None,
) -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` backed by ``MockTransport``."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=json_body, request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _settings(**overrides: object) -> Settings:
    """Build a ``Settings`` instance with a configured OpenRouter key.

    ``Settings`` fields declare ``validation_alias`` (e.g.
    ``AliasChoices("REPOACH_OPENROUTER_API_KEY", "OPENROUTER_API_KEY")``)
    and the model has no ``populate_by_name``, so constructor kwargs must
    use one of the alias spellings — the bare field name is silently
    dropped by ``extra="ignore"`` and the default survives instead.
    """
    values: dict[str, object] = {
        "_env_file": None,
        "OPENROUTER_API_KEY": "test-token",
        "CREDITS_FLOOR_USD": 2.0,
        "CREDITS_HEALTH_CACHE_TTL_S": 3600.0,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(autouse=True)
def _cold_cache() -> None:
    """Reset the credits cache before each test."""
    reset_credits_cache()


async def test_below_floor_excludes_open_router_refs() -> None:
    """remaining < floor excludes every open_router ref from dispatch."""
    settings = _settings()
    payload = {"data": {"total_credits": 20.0, "total_usage": 20.21}}
    client = _make_client(json_body=payload)

    result = await compute_credits_gate_skip_models(settings, client, _OPEN_ROUTER_REFS)

    assert result == _OPEN_ROUTER_REFS


async def test_at_floor_keeps_open_router() -> None:
    """remaining == floor is NOT below it (strict less-than) — gate stays open."""
    settings = _settings()
    payload = {"data": {"total_credits": 22.0, "total_usage": 20.0}}
    client = _make_client(json_body=payload)

    result = await compute_credits_gate_skip_models(settings, client, _OPEN_ROUTER_REFS)

    assert result == frozenset()


async def test_snapshot_unavailable_fails_open() -> None:
    """A failing probe (500) yields an empty exclusion set, never a bench."""
    settings = _settings()
    client = _make_client(status_code=500, json_body={"error": "internal"})

    result = await compute_credits_gate_skip_models(settings, client, _OPEN_ROUTER_REFS)

    assert result == frozenset()


async def test_recovered_balance_lifts_gate_without_restart() -> None:
    """A low-balance call excludes; a fresh above-floor snapshot later lifts it."""
    settings = _settings()
    low_payload = {"data": {"total_credits": 20.0, "total_usage": 20.21}}
    low_client = _make_client(json_body=low_payload)

    first = await compute_credits_gate_skip_models(settings, low_client, _OPEN_ROUTER_REFS)

    assert first == _OPEN_ROUTER_REFS

    from repoach.health import credits as credits_module

    credits_module._cached_fetched_at = time.monotonic() - settings.credits_health_cache_ttl_s - 1.0
    recovered_payload = {"data": {"total_credits": 25.0, "total_usage": 5.0}}
    recovered_client = _make_client(json_body=recovered_payload)

    second = await compute_credits_gate_skip_models(settings, recovered_client, _OPEN_ROUTER_REFS)

    assert second == frozenset()


def test_service_open_router_refs_for_delegates_to_router() -> None:
    """ClaudeProxyService.open_router_refs_for delegates to ModelRouter."""
    settings = _settings(MODEL_SONNET="open_router/qwen/qwen3.7-max,nvidia_nim/z-ai/glm4.7")
    service = ClaudeProxyService(settings, provider_getter=lambda provider_type: None)

    result = service.open_router_refs_for("claude-sonnet-4-20250514")

    assert result == frozenset({"open_router/qwen/qwen3.7-max"})
