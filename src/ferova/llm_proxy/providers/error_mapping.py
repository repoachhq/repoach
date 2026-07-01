"""Provider-specific exception mapping."""

import httpx
import openai

from ferova.llm_proxy.core.anthropic import get_user_facing_error_message
from ferova.llm_proxy.providers.exceptions import (
    APIError,
    AuthenticationError,
    InvalidRequestError,
    OverloadedError,
    RateLimitError,
)
from ferova.llm_proxy.providers.rate_limit import GlobalRateLimiter


def map_error(e: Exception, *, rate_limiter: GlobalRateLimiter | None = None) -> Exception:
    """Map OpenAI or HTTPX exception to specific ProviderError."""
    message = get_user_facing_error_message(e)
    limiter = rate_limiter or GlobalRateLimiter.get_instance()

    if isinstance(e, openai.AuthenticationError):
        return AuthenticationError(message, raw_error=str(e))
    if isinstance(e, openai.RateLimitError):
        limiter.set_blocked(60)
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
            limiter.set_blocked(60)
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
    mapped = map_error(error, rate_limiter=rate_limiter)
    if getattr(mapped, "status_code", None) == 405:
        return (
            f"Upstream provider {provider_name} rejected the request method or endpoint (HTTP 405)."
        )
    return get_user_facing_error_message(mapped, read_timeout_s=read_timeout_s)
