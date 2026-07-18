"""Unit tests for SP-CREDITS-CHECK — the OpenRouter credits floor probe.

Uses ``httpx.AsyncClient(transport=httpx.MockTransport(...))`` as the
truthful boundary fake — no monkeypatching of ferova code.
"""

from __future__ import annotations

import time

import httpx
import pytest
from pydantic import ValidationError

from ferova.health.credits import (
    CreditsSnapshot,
    fetch_openrouter_credits,
    get_cached_credits,
    reset_credits_cache,
)

_NOMINAL_PAYLOAD = {"data": {"total_credits": 20.0, "total_usage": 19.5}}


def _make_client(
    *,
    status_code: int = 200,
    json_body: object = None,
    raise_exc: Exception | None = None,
) -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` backed by ``MockTransport``."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if raise_exc is not None:
            raise raise_exc
        body = json_body
        return httpx.Response(status_code, json=body, request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _cold_cache() -> None:
    """Reset the credits cache before each test."""
    reset_credits_cache()


async def test_fetch_nominal_returns_snapshot() -> None:
    """Nominal payload produces the expected CreditsSnapshot."""
    client = _make_client(json_body=_NOMINAL_PAYLOAD)

    result = await fetch_openrouter_credits("test-key", client=client)

    assert result is not None
    assert result.total_credits == 20.0
    assert result.total_usage == 19.5
    assert result.remaining == 0.5


@pytest.mark.parametrize(
    "status_code,body,desc",
    [
        (500, {"error": "internal"}, "500"),
        (200, "not json", "malformed_json"),
        (200, {"data": "not a dict"}, "data_not_dict"),
        (200, {"data": {"total_credits": 1.0}}, "missing_total_usage"),
        (200, {"data": {"total_usage": 1.0}}, "missing_total_credits"),
        (200, {"data": {"total_credits": "str", "total_usage": 1.0}}, "non_numeric_credits"),
    ],
)
async def test_fetch_errors_return_none(
    status_code: int,
    body: object,
    desc: str,
) -> None:
    """Parametrized failures all return None without raising."""
    client = _make_client(status_code=status_code, json_body=body)

    result = await fetch_openrouter_credits("test-key", client=client)

    assert result is None, f"expected None for {desc}"


async def test_fetch_timeout_returns_none() -> None:
    """A transport-level timeout returns None without raising."""
    client = _make_client(raise_exc=httpx.ReadTimeout("timed out"))

    result = await fetch_openrouter_credits("test-key", client=client)

    assert result is None


async def test_cache_ttl_expiry_and_reset() -> None:
    """Cache hit, expiry, refresh, failure-caching, and reset all work."""
    client = _make_client(json_body=_NOMINAL_PAYLOAD)
    ttl_s = 3600.0

    snapshot1 = await get_cached_credits("test-key", client=client, ttl_s=ttl_s, timeout_s=10.0)
    assert snapshot1 is not None
    assert snapshot1.remaining == 0.5

    snapshot2 = await get_cached_credits("test-key", client=client, ttl_s=ttl_s, timeout_s=10.0)
    assert snapshot2 is snapshot1

    cached_time = time.monotonic()
    from ferova.health import credits as credits_module

    credits_module._cached_fetched_at = cached_time - ttl_s - 1.0

    updated_payload = {"data": {"total_credits": 25.0, "total_usage": 20.0}}
    client2 = _make_client(json_body=updated_payload)
    snapshot3 = await get_cached_credits("test-key", client=client2, ttl_s=ttl_s, timeout_s=10.0)
    assert snapshot3 is not None
    assert snapshot3.total_credits == 25.0

    credits_module._cached_fetched_at = cached_time - ttl_s - 1.0
    fail_client = _make_client(status_code=500, json_body={})
    snapshot4 = await get_cached_credits(
        "test-key", client=fail_client, ttl_s=ttl_s, timeout_s=10.0
    )
    assert snapshot4 is None

    good_client = _make_client(json_body=_NOMINAL_PAYLOAD)
    snapshot5 = await get_cached_credits(
        "test-key", client=good_client, ttl_s=ttl_s, timeout_s=10.0
    )
    assert snapshot5 is None

    credits_module._cached_fetched_at = cached_time - 61.0
    snapshot6 = await get_cached_credits(
        "test-key", client=good_client, ttl_s=ttl_s, timeout_s=10.0
    )
    assert snapshot6 is not None
    assert snapshot6.remaining == 0.5

    reset_credits_cache()
    credits_module._cached_fetched_at = cached_time - 1.0
    snapshot7 = await get_cached_credits(
        "test-key", client=good_client, ttl_s=ttl_s, timeout_s=10.0
    )
    assert snapshot7 is not None


async def test_remaining_can_be_negative() -> None:
    """When usage exceeds credits, remaining is negative (not clamped)."""
    payload = {"data": {"total_credits": 20.0, "total_usage": 20.21}}
    client = _make_client(json_body=payload)

    result = await fetch_openrouter_credits("test-key", client=client)

    assert result is not None
    assert result.remaining == pytest.approx(-0.21)


async def test_snapshot_is_frozen() -> None:
    """CreditsSnapshot is immutable (frozen pydantic model)."""
    snapshot = CreditsSnapshot(total_credits=10.0, total_usage=5.0)

    with pytest.raises(ValidationError):
        snapshot.total_credits = 15.0

    assert snapshot.total_credits == 10.0
