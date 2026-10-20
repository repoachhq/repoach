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


class ProviderConfigError(GatewayConfigError):
    """The selected provider cannot be used: a missing CLI, login or key (rc 2).

    Subclasses :class:`~repoach.core.config.GatewayConfigError` so every
    command that already turns a missing gateway into one stderr line and
    exit code 2 does the same for a direct provider.
    """


__all__ = [
    "GatewayChainExhausted",
    "GatewayError",
    "GatewayTransportError",
    "ProviderConfigError",
]
