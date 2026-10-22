"""SP-FAILOVER-SKIPS-BLOCKED-PROVIDER: ``map_error`` honours ``Retry-After``.

Before this spec every 429 blocked its provider's scoped
``GlobalRateLimiter`` for a hardcoded 60 seconds
(``providers/error_mapping.py``). These tests pin the new behavior: a
``Retry-After`` header (delta-seconds form) sizes the block instead,
capped at ``rate_limit_block_max_s``, and a 429 with no usable header
still falls back to ``rate_limit_block_default_s`` unchanged.
"""

from __future__ import annotations

import httpx

from repoach.llm_proxy.providers.error_mapping import map_error
from repoach.llm_proxy.providers.rate_limit import GlobalRateLimiter


def _rate_limit_error(
    *, retry_after: str | None, scope: str
) -> tuple[httpx.HTTPStatusError, GlobalRateLimiter]:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    response = httpx.Response(429, headers=headers, request=request)
    error = httpx.HTTPStatusError("429 rate limited", request=request, response=response)
    limiter = GlobalRateLimiter.get_scoped_instance(scope)
    return error, limiter


def test_429_without_retry_after_uses_default() -> None:
    error, limiter = _rate_limit_error(retry_after=None, scope="error-mapping-default")
    map_error(error, rate_limiter=limiter, rate_limit_block_default_s=60.0)
    remaining = limiter.seconds_until_unblocked()
    assert 58.0 <= remaining <= 60.5, remaining


def test_429_with_retry_after_blocks_for_that_duration() -> None:
    error, limiter = _rate_limit_error(retry_after="5", scope="error-mapping-retry-after-5")
    map_error(error, rate_limiter=limiter)
    remaining = limiter.seconds_until_unblocked()
    assert 3.5 <= remaining <= 5.5, remaining


def test_retry_after_above_cap_is_clamped() -> None:
    error, limiter = _rate_limit_error(retry_after="500", scope="error-mapping-retry-after-cap")
    map_error(error, rate_limiter=limiter, rate_limit_block_max_s=120.0)
    remaining = limiter.seconds_until_unblocked()
    assert remaining <= 120.5, remaining


def test_retry_after_non_numeric_falls_back_to_default() -> None:
    error, limiter = _rate_limit_error(
        retry_after="Wed, 21 Oct 2026 07:28:00 GMT", scope="error-mapping-retry-after-date"
    )
    map_error(error, rate_limiter=limiter, rate_limit_block_default_s=45.0)
    remaining = limiter.seconds_until_unblocked()
    assert 43.0 <= remaining <= 45.5, remaining


def test_retry_after_zero_falls_back_to_default() -> None:
    error, limiter = _rate_limit_error(retry_after="0", scope="error-mapping-retry-after-zero")
    map_error(error, rate_limiter=limiter, rate_limit_block_default_s=30.0)
    remaining = limiter.seconds_until_unblocked()
    assert 28.0 <= remaining <= 30.5, remaining
