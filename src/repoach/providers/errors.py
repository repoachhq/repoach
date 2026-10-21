"""Errors every model provider raises to the agent loop (SP-UNIVERSAL-PROVIDERS).

The three ``Gateway*`` classes predate the direct providers: the agent
loop and the reviewers' backoff catch them by these names, so a direct
provider raises the same types and every retry and verdict rule
(SP-REVIEW-TRANSPORT-ERROR-VERDICT) applies unchanged.
:mod:`repoach.agent_engine.adapters` re-exports them.
"""

from __future__ import annotations

from ..core.config import GatewayConfigError


class GatewayError(Exception):
    """Base class for provider errors raised to the agent loop (not retried)."""


class GatewayTransportError(GatewayError):
    """5xx, 429, connect-refused, read-timeout — retryable transport-level."""


class GatewayChainExhausted(GatewayError):
    """The proxy walked its chain and every upstream returned an error.

    Distinct from :class:`GatewayTransportError` because the failover
    is the proxy's job — when this fires, the proxy already gave up
    on every entry in ``MODEL_<CAPABILITY>``.  Retrying from the
    agent side won't help.
    """


class ProviderAuthError(GatewayError):
    """The provider rejected the credential (HTTP 401/403, a logged-out CLI).

    ``retryable = False`` makes every retry loop
    (:func:`~repoach.review.retry_backoff.retry_with_backoff`) stop at the
    first attempt: retrying repeats the refusal and loses the hint.  The
    message names what to replace or run, never a value.
    """

    retryable = False


class ProviderRequestError(GatewayError):
    """The provider refused the request itself (HTTP 400, 404, 422).

    An unknown model id, a wrong path or a body the endpoint rejects: the
    same request fails the same way, so ``retryable = False`` stops
    every retry loop at the first attempt, like :class:`ProviderAuthError`.
    """

    retryable = False


class ProviderConfigError(GatewayConfigError):
    """The selected provider cannot be used: a missing CLI, login or key (rc 2).

    Subclasses :class:`~repoach.core.config.GatewayConfigError` so every
    command that already turns a missing gateway into one stderr line and
    exit code 2 does the same for a direct provider.
    """


NO_PROVIDER_MESSAGE = (
    "no provider configured -- pass --provider, or set REPOACH_PROVIDER or [provider].kind "
    "in repoach.toml, to a kind (claude_code, anthropic, openai_compatible, gemini, gateway) "
    "or a preset (openai, ollama, openrouter, groq, deepseek, mistral)"
)
"""The one line every command prints, after ``repoach: ``, when nothing names a provider."""


class NoProviderConfiguredError(ProviderConfigError):
    """No ``--provider``, no ``REPOACH_PROVIDER``, no ``[provider].kind`` and no gateway URL (rc 2).

    One class and one message (:data:`NO_PROVIDER_MESSAGE`) for every
    command, raised before any forge call (SP-UNIVERSAL-PROVIDERS G4).
    """

    def __init__(self) -> None:
        super().__init__(NO_PROVIDER_MESSAGE)


__all__ = [
    "NO_PROVIDER_MESSAGE",
    "GatewayChainExhausted",
    "GatewayError",
    "GatewayTransportError",
    "NoProviderConfiguredError",
    "ProviderAuthError",
    "ProviderConfigError",
    "ProviderRequestError",
]
