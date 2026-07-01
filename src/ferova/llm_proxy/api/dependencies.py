"""Dependency injection for FastAPI."""

import secrets

from fastapi import Depends, HTTPException, Request
from loguru import logger
from starlette.applications import Starlette

from ferova.llm_proxy.config.settings import Settings
from ferova.llm_proxy.config.settings import get_settings as _get_settings
from ferova.llm_proxy.core.anthropic import get_user_facing_error_message
from ferova.llm_proxy.providers.base import BaseProvider
from ferova.llm_proxy.providers.exceptions import AuthenticationError, UnknownProviderTypeError
from ferova.llm_proxy.providers.registry import PROVIDER_DESCRIPTORS, ProviderRegistry

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
    supported, and any suffix after the first ``:`` is stripped to
    accept tokens with appended model names.  The comparison is
    constant-time (``secrets.compare_digest``) so the token cannot
    be recovered byte-by-byte through timing.
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

    if token and ":" in token:
        token = token.split(":", 1)[0]

    if not secrets.compare_digest(token.encode(), anthropic_auth_token.encode()):
        raise HTTPException(status_code=401, detail="Invalid API key")
