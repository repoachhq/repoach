"""Dependency injection for FastAPI."""

import secrets

import httpx
from fastapi import Depends, HTTPException, Request
from loguru import logger
from starlette.applications import Starlette

from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.config.settings import get_settings as _get_settings
from repoach.llm_proxy.core.anthropic import get_user_facing_error_message
from repoach.llm_proxy.providers.base import BaseProvider
from repoach.llm_proxy.providers.exceptions import AuthenticationError, UnknownProviderTypeError
from repoach.llm_proxy.providers.registry import PROVIDER_DESCRIPTORS, ProviderRegistry

_providers: dict[str, BaseProvider] = {}
"""Process-level provider cache — the non-HTTP fallback used by
:func:`resolve_provider` when no ``app`` is supplied.  HTTP handlers
always pass ``app`` so the app-scoped :class:`ProviderRegistry` is used
instead.
"""


def get_settings() -> Settings:
    """Get application settings via dependency injection."""
    return _get_settings()


def resolve_provider(
    provider_type: str,
    *,
    app: Starlette | None,
    settings: Settings,
) -> BaseProvider:
    """Resolve a provider using the app-scoped registry when ``app`` is set.

    When ``app`` is not ``None``, the app-owned :attr:`app.state.provider_registry`
    is always used. If the registry is missing (e.g. a test app without
    :class:`~api.runtime.AppRuntime` startup), a new :class:`ProviderRegistry`
    is installed on ``app.state`` so the process cache is never mixed with
    per-request app identity.

    When ``app`` is ``None`` (no HTTP context), uses the process-level
    :data:`_providers` cache only.
    """
    if app is not None:
        reg = getattr(app.state, "provider_registry", None)
        if reg is None:
            reg = ProviderRegistry()
            app.state.provider_registry = reg
        return _resolve_with_registry(reg, provider_type, settings)
    return _resolve_with_registry(ProviderRegistry(_providers), provider_type, settings)


def _resolve_with_registry(
    registry: ProviderRegistry, provider_type: str, settings: Settings
) -> BaseProvider:
    should_log_init = not registry.is_cached(provider_type)
    try:
        provider = registry.get(provider_type, settings)
    except AuthenticationError as e:
        raise HTTPException(status_code=503, detail=get_user_facing_error_message(e)) from e
    except UnknownProviderTypeError:
        logger.error(
            "Unknown provider_type: '{}'. Supported: {}",
            provider_type,
            ", ".join(f"'{key}'" for key in PROVIDER_DESCRIPTORS),
        )
        raise
    if should_log_init:
        logger.info("Provider initialized: {}", provider_type)
    return provider


def require_api_key(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """Require a server API key (Anthropic-style).

    Checks ``x-api-key`` header or ``Authorization: Bearer ...``
    against ``Settings.anthropic_auth_token``.  If
    ``ANTHROPIC_AUTH_TOKEN`` is empty, this is a no-op (no API key
    configured → every request is allowed).  Both raw keys in
    ``X-API-Key`` and bearer tokens in ``Authorization`` are
    supported.  A presented credential of the form
    ``f"{token}:{suffix}"`` also authenticates, but only when the
    leading ``len(token)`` characters are an exact, constant-time
    match for the configured token and ``suffix`` is non-empty —
    a colon anywhere else in the candidate is not a delimiter
    (SP-PROXY-EDGE-HARDEN tightens the prior first-colon truncation,
    which matched the presented value against the configured token
    after cutting at the FIRST ``:`` regardless of where it fell).
    Every comparison is constant-time (``secrets.compare_digest``)
    so the token cannot be recovered byte-by-byte through timing.
    """
    anthropic_auth_token = settings.anthropic_auth_token
    if not anthropic_auth_token:
        return

    header = (
        request.headers.get("x-api-key")
        or request.headers.get("authorization")
        or request.headers.get("anthropic-auth-token")
    )
    if not header:
        raise HTTPException(status_code=401, detail="Missing API key")

    token = header
    if header.lower().startswith("bearer "):
        token = header.split(" ", 1)[1]

    if secrets.compare_digest(token.encode(), anthropic_auth_token.encode()):
        return

    suffix_prefix = f"{anthropic_auth_token}:"
    candidate_prefix = token[: len(suffix_prefix)]
    if len(token) > len(suffix_prefix) and secrets.compare_digest(
        candidate_prefix.encode(), suffix_prefix.encode()
    ):
        return

    raise HTTPException(status_code=401, detail="Invalid API key")


def is_authenticated(request: Request, settings: Settings = Depends(get_settings)) -> bool:
    """Return whether the request would pass :func:`require_api_key`.

    Reuses the same header/token matching without raising — used by
    endpoints such as ``GET /health`` that expose a minimal
    unauthenticated liveness surface plus authenticated detail
    (SP-PROXY-EDGE-HARDEN F-HEALTH): an anonymous caller sees liveness
    only, an authenticated one (or any caller when no token is
    configured) sees the full body.
    """
    try:
        require_api_key(request, settings)
    except HTTPException as exc:
        logger.debug("proxy_health_detail_denied: status_code={}", exc.status_code)
        return False
    return True


def get_credits_client() -> httpx.AsyncClient:
    """Return an httpx.AsyncClient for credits health checks.

    Overridable via ``app.dependency_overrides`` in tests so the
    /health endpoint can be driven through a
    :class:`httpx.MockTransport` without live network calls.
    """
    return httpx.AsyncClient()
