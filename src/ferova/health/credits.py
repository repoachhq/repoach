"""OpenRouter credits floor probe — SP-CREDITS-CHECK."""

from __future__ import annotations

import time

import httpx
import structlog
from pydantic import BaseModel

_log = structlog.get_logger(__name__)
_cached_snapshot: CreditsSnapshot | None = None
_cached_fetched_at: float | None = None
_cached_is_failure: bool = False


class CreditsSnapshot(BaseModel):
    """Frozen OpenRouter credit balance with a computed ``remaining`` property.

    Attributes:
        total_credits: Total credits allocated to the account.
        total_usage: Total credits consumed so far.
    """

    model_config = {"frozen": True}

    total_credits: float
    total_usage: float

    @property
    def remaining(self) -> float:
        """Credits remaining — not clamped, may be negative."""
        return self.total_credits - self.total_usage


async def fetch_openrouter_credits(
    api_key: str, *, client: httpx.AsyncClient, timeout_s: float = 10.0
) -> CreditsSnapshot | None:
    """GET ``/api/v1/credits``; returns snapshot or ``None``, never raises."""
    try:
        response = await client.get(
            "https://openrouter.ai/api/v1/credits",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
        )
        if response.status_code != 200:
            _log.warning(
                "openrouter_credits_unavailable", status_code=response.status_code, reason="non-2xx"
            )
            return None
        data = response.json().get("data")
        if not isinstance(data, dict):
            _log.warning("openrouter_credits_unavailable", reason="unexpected_payload_shape")
            return None
        tc, tu = data.get("total_credits"), data.get("total_usage")
        if not isinstance(tc, (int, float)) or not isinstance(tu, (int, float)):
            _log.warning("openrouter_credits_unavailable", reason="missing_keys_or_non_numeric")
            return None
        return CreditsSnapshot(total_credits=float(tc), total_usage=float(tu))
    except Exception:
        _log.warning("openrouter_credits_unavailable", reason="transport_error")
        return None


async def get_cached_credits(
    api_key: str, *, client: httpx.AsyncClient, ttl_s: float, timeout_s: float = 3.0
) -> CreditsSnapshot | None:
    """TTL-cached credits snapshot; caches failures for ``min(60, ttl_s)`` s."""
    global _cached_snapshot, _cached_fetched_at, _cached_is_failure
    now = time.monotonic()
    if _cached_fetched_at is not None:
        if _cached_is_failure and (now - _cached_fetched_at) < min(60.0, ttl_s):
            return None
        if (
            not _cached_is_failure
            and _cached_snapshot is not None
            and (now - _cached_fetched_at) < ttl_s
        ):
            return _cached_snapshot
    result = await fetch_openrouter_credits(api_key, client=client, timeout_s=timeout_s)
    _cached_fetched_at = now
    if result is None:
        _cached_is_failure = True
        _cached_snapshot = None
        return None
    _cached_is_failure = False
    _cached_snapshot = result
    return result


def reset_credits_cache() -> None:
    """Clear the module-level credits cache."""
    global _cached_snapshot, _cached_fetched_at, _cached_is_failure
    _cached_snapshot = None
    _cached_fetched_at = None
    _cached_is_failure = False
