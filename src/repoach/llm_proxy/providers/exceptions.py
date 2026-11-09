"""Unified exception hierarchy for providers."""

from typing import Any


class ProviderError(Exception):
    """Base exception for all provider errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        error_type: str = "api_error",
        raw_error: Any = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type
        self.raw_error = raw_error

    def to_anthropic_format(self) -> dict:
        """Convert to Anthropic-compatible error response."""
        return {
            "type": "error",
            "error": {
                "type": self.error_type,
                "message": self.message,
            },
        }


class AuthenticationError(ProviderError):
    """Raised when API key is invalid or missing."""

    def __init__(self, message: str, raw_error: Any = None):
        super().__init__(
            message,
            status_code=401,
            error_type="authentication_error",
            raw_error=raw_error,
        )


class InvalidRequestError(ProviderError):
    """Raised when the request parameters are invalid."""

    def __init__(self, message: str, raw_error: Any = None):
        super().__init__(
            message,
            status_code=400,
            error_type="invalid_request_error",
            raw_error=raw_error,
        )


class RateLimitError(ProviderError):
    """Raised when rate limit is exceeded."""

    def __init__(self, message: str, raw_error: Any = None):
        super().__init__(
            message,
            status_code=429,
            error_type="rate_limit_error",
            raw_error=raw_error,
        )


class OverloadedError(ProviderError):
    """Raised when the provider is overloaded."""

    def __init__(self, message: str, raw_error: Any = None):
        super().__init__(
            message,
            status_code=529,
            error_type="overloaded_error",
            raw_error=raw_error,
        )


class APIError(ProviderError):
    """Raised when the provider returns a generic API error."""

    def __init__(self, message: str, status_code: int = 500, raw_error: Any = None):
        super().__init__(
            message,
            status_code=status_code,
            error_type="api_error",
            raw_error=raw_error,
        )


class UpstreamStatusError(ProviderError):
    """A recovered-status-only error rebuilt on the dispatcher side of the wire.

    The HTTP transports (``openai_compat.py``, ``anthropic_messages.py``)
    already map the real upstream exception via
    :func:`repoach.llm_proxy.providers.error_mapping.map_error` and thread
    its ``status_code`` onto the terminal SSE ``message_delta`` before
    degrading to an in-band error event (SP-BREAKER-LIVE-REASONS); by the
    time the chain-failover dispatcher inspects the drained stream, the
    original exception object itself is gone. This type lets
    ``_classify_failover_reason`` recover the same failover vocabulary a
    live raised exception would have produced from just that recovered
    integer, without re-deriving it from unstructured SSE text.
    """

    def __init__(self, status_code: int):
        super().__init__(
            f"Upstream request failed with HTTP {status_code}.",
            status_code=status_code,
            error_type="upstream_status_error",
        )


class OutputCapExceededError(ProviderError):
    """The model's reply outgrew the output cap it was given: the request's shape, not the network.

    SP-REVIEW-SURVIVES-TRANSPORT-FAILURES D6. On 2026-11-04 the Sentinel's
    ``claude -p`` call on #288 (a 118 857-character prompt) spent four
    replies of 4 096 thinking tokens each and exited 1 with ``"error":
    "max_output_tokens"``: ``Claude's response exceeded the 4096 output
    token maximum``. The same request fails the same way, so the
    candidate is not tripped and the caller is told not to repeat it.
    """

    def __init__(self, message: str, raw_error: Any = None):
        super().__init__(
            message,
            status_code=422,
            error_type="output_cap_exceeded",
            raw_error=raw_error,
        )


class UnknownProviderTypeError(ValueError):
    """Raised when ``provider_id`` is not registered in the provider map."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
