"""Provider-specific exception mapping."""

import httpx
import openai
from loguru import logger

from repoach.llm_proxy.core.anthropic import get_user_facing_error_message
from repoach.llm_proxy.providers.exceptions import (
    APIError,
    AuthenticationError,
    InvalidRequestError,
    OverloadedError,
    RateLimitError,
)
from repoach.llm_proxy.providers.rate_limit import GlobalRateLimiter


def _retry_after_seconds(e: Exception) -> float | None:
    """Extract a numeric ``Retry-After`` seconds value from a 429, if present.

    Both ``httpx.HTTPStatusError`` and ``openai.APIStatusError`` (the
    base of ``openai.RateLimitError``) expose the raw upstream
    ``httpx.Response`` as ``.response``. Only the delta-seconds form of
    ``Retry-After`` is honoured — every provider observed in practice
    sends that form ; an HTTP-date value, or a missing/malformed
    header, falls back to the caller's default block duration.
    """
    response = getattr(e, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError) as exc:
        logger.debug("Ignoring unparsable Retry-After header {!r}: {}", raw, exc)
        return None
    return seconds if seconds > 0 else None


def _rate_limit_block_seconds(e: Exception, *, default_s: float, max_s: float) -> float:
    """Pick the reactive block duration for a 429: ``Retry-After``, capped, else ``default_s``."""
    retry_after = _retry_after_seconds(e)
    seconds = retry_after if retry_after is not None else default_s
    return min(seconds, max_s)


def map_error(
    e: Exception,
    *,
    rate_limiter: GlobalRateLimiter | None = None,
    rate_limit_block_default_s: float = 60.0,
    rate_limit_block_max_s: float = 300.0,
) -> Exception:
    """Map OpenAI or HTTPX exception to specific ProviderError.

    A 429 blocks ``limiter`` for the upstream's ``Retry-After`` value
    when present (capped at ``rate_limit_block_max_s``), else for
    ``rate_limit_block_default_s`` (SP-FAILOVER-SKIPS-BLOCKED-PROVIDER).
    """
    message = get_user_facing_error_message(e)
    limiter = rate_limiter or GlobalRateLimiter.get_scoped_instance("error_mapping_default")

    if isinstance(e, openai.AuthenticationError):
        return AuthenticationError(message, raw_error=str(e))
    if isinstance(e, openai.RateLimitError):
        limiter.set_blocked(
            _rate_limit_block_seconds(
                e, default_s=rate_limit_block_default_s, max_s=rate_limit_block_max_s
            )
        )
        return RateLimitError(message, raw_error=str(e))
    if isinstance(e, openai.BadRequestError):
        return InvalidRequestError(message, raw_error=str(e))
    if isinstance(e, openai.InternalServerError):
        raw_message = str(e)
        if "overloaded" in raw_message.lower() or "capacity" in raw_message.lower():
            return OverloadedError(message, raw_error=raw_message)
        return APIError(message, status_code=500, raw_error=str(e))
    if isinstance(e, openai.APIError):
        return APIError(message, status_code=getattr(e, "status_code", 500), raw_error=str(e))

    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status in (401, 403):
            return AuthenticationError(message, raw_error=str(e))
        if status == 429:
            limiter.set_blocked(
                _rate_limit_block_seconds(
                    e, default_s=rate_limit_block_default_s, max_s=rate_limit_block_max_s
                )
            )
            return RateLimitError(message, raw_error=str(e))
        if status == 400:
            return InvalidRequestError(message, raw_error=str(e))
        if status >= 500:
            if status in (502, 503, 504):
                return OverloadedError(message, raw_error=str(e))
            return APIError(message, status_code=status, raw_error=str(e))
        return APIError(message, status_code=status, raw_error=str(e))

    return e


def provider_error_message(
    error: Exception,
    *,
    provider_name: str,
    rate_limiter: GlobalRateLimiter | None = None,
    read_timeout_s: float,
    rate_limit_block_default_s: float = 60.0,
    rate_limit_block_max_s: float = 300.0,
) -> str:
    """Map an upstream exception to the base user-facing provider error text.

    Consolidates the error-to-text path both transports emit verbatim on an
    upstream failure: an HTTP 405 reads as the upstream rejecting the request
    method or endpoint, anything else falls back to the read-timeout-aware
    message. The returned text carries no request-id suffix — each transport
    appends its own (the OpenAI-chat shape via ``append_request_id``, the
    native Anthropic shape via ``Request ID:``), so that divergence stays
    local to the caller.

    Args:
        error: The raw upstream exception.
        provider_name: The provider tag named in the 405 message.
        rate_limiter: The provider's limiter, threaded into :func:`map_error`
            so a 429 marks it blocked.
        read_timeout_s: The configured read timeout, surfaced in the timeout
            message.

    Returns:
        The base user-facing message, without a request-id suffix.
    """
    mapped = map_error(
        error,
        rate_limiter=rate_limiter,
        rate_limit_block_default_s=rate_limit_block_default_s,
        rate_limit_block_max_s=rate_limit_block_max_s,
    )
    if getattr(mapped, "status_code", None) == 405:
        return (
            f"Upstream provider {provider_name} rejected the request method or endpoint (HTTP 405)."
        )
    return get_user_facing_error_message(mapped, read_timeout_s=read_timeout_s)
