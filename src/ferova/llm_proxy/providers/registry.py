"""Provider descriptors, factory, and runtime registry."""

from __future__ import annotations

from collections.abc import Callable, MutableMapping

from ferova.llm_proxy.config.settings import Settings
from ferova.llm_proxy.providers.base import BaseProvider, ProviderConfig
from ferova.llm_proxy.providers.catalog import (
    PROVIDER_DESCRIPTORS,
    SUPPORTED_PROVIDER_IDS,
    ProviderDescriptor,
    TransportType,
)
from ferova.llm_proxy.providers.exceptions import AuthenticationError, UnknownProviderTypeError

__all__ = [
    "PROVIDER_DESCRIPTORS",
    "SUPPORTED_PROVIDER_IDS",
    "ProviderDescriptor",
    "ProviderFactory",
    "ProviderRegistry",
    "TransportType",
    "build_provider_config",
    "create_provider",
]

ProviderFactory = Callable[[ProviderConfig, Settings], BaseProvider]
TransportBuilder = Callable[[ProviderConfig, ProviderDescriptor], BaseProvider]


def _create_nvidia_nim(config: ProviderConfig, settings: Settings) -> BaseProvider:
    from ferova.llm_proxy.providers.nvidia_nim import NvidiaNimProvider

    return NvidiaNimProvider(config, nim_settings=settings.nim)


def _create_open_router(config: ProviderConfig, _settings: Settings) -> BaseProvider:
    from ferova.llm_proxy.providers.open_router import OpenRouterProvider

    return OpenRouterProvider(config)


def _create_claude_code(config: ProviderConfig, settings: Settings) -> BaseProvider:
    from ferova.llm_proxy.providers.claude_code import ClaudeCodeProvider

    return ClaudeCodeProvider(
        config,
        cli_path=settings.claude_code_cli_path,
        default_model=settings.claude_code_default_model,
        subprocess_timeout=settings.claude_code_subprocess_timeout,
    )


def _build_generic_openai(config: ProviderConfig, descriptor: ProviderDescriptor) -> BaseProvider:
    from ferova.llm_proxy.providers.openai_generic import GenericOpenAIProvider

    return GenericOpenAIProvider(config, provider_name=descriptor.provider_id)


_BESPOKE_FACTORIES: dict[str, ProviderFactory] = {
    "nvidia_nim": _create_nvidia_nim,
    "open_router": _create_open_router,
    "claude_code": _create_claude_code,
}

_GENERIC_TRANSPORT_BUILDERS: dict[TransportType, TransportBuilder] = {
    "openai_chat": _build_generic_openai,
}


_UNBUILDABLE_DESCRIPTORS: dict[str, str] = {
    provider_id: descriptor.transport_type
    for provider_id, descriptor in PROVIDER_DESCRIPTORS.items()
    if provider_id not in _BESPOKE_FACTORIES
    and descriptor.transport_type not in _GENERIC_TRANSPORT_BUILDERS
}


if _UNBUILDABLE_DESCRIPTORS:
    raise AssertionError(
        "Every provider descriptor must be buildable — these have neither a "
        "bespoke factory nor a generic transport builder for their "
        f"transport_type: {_UNBUILDABLE_DESCRIPTORS!r}"
    )


def _string_attr(settings: Settings, attr_name: str | None, default: str = "") -> str:
    if attr_name is None:
        return default
    value = getattr(settings, attr_name, default)
    return value if isinstance(value, str) else default


def _credential_for(descriptor: ProviderDescriptor, settings: Settings) -> str:
    if descriptor.static_credential is not None:
        return descriptor.static_credential
    if descriptor.credential_attr:
        return _string_attr(settings, descriptor.credential_attr)
    return ""


def _require_credential(descriptor: ProviderDescriptor, credential: str) -> None:
    if descriptor.credential_env is None:
        return
    if credential and credential.strip():
        return
    message = f"{descriptor.credential_env} is not set. Add it to your .env file."
    if descriptor.credential_url:
        message = f"{message} Get a key at {descriptor.credential_url}"
    raise AuthenticationError(message)


def build_provider_config(descriptor: ProviderDescriptor, settings: Settings) -> ProviderConfig:
    credential = _credential_for(descriptor, settings)
    _require_credential(descriptor, credential)
    base_url = _string_attr(settings, descriptor.base_url_attr, descriptor.default_base_url or "")
    proxy = _string_attr(settings, descriptor.proxy_attr)
    return ProviderConfig(
        api_key=credential,
        base_url=base_url or descriptor.default_base_url,
        rate_limit=settings.provider_rate_limit,
        rate_window=settings.provider_rate_window,
        max_concurrency=settings.provider_max_concurrency,
        http_read_timeout=settings.http_read_timeout,
        http_write_timeout=settings.http_write_timeout,
        http_connect_timeout=settings.http_connect_timeout,
        enable_thinking=settings.enable_thinking,
        proxy=proxy,
    )


def create_provider(provider_id: str, settings: Settings) -> BaseProvider:
    descriptor = PROVIDER_DESCRIPTORS.get(provider_id)
    if descriptor is None:
        supported = "', '".join(PROVIDER_DESCRIPTORS)
        raise UnknownProviderTypeError(
            f"Unknown provider_type: '{provider_id}'. Supported: '{supported}'"
        )

    config = build_provider_config(descriptor, settings)

    bespoke = _BESPOKE_FACTORIES.get(provider_id)
    if bespoke is not None:
        return bespoke(config, settings)

    builder = _GENERIC_TRANSPORT_BUILDERS.get(descriptor.transport_type)
    if builder is None:
        raise UnknownProviderTypeError(
            f"Provider '{provider_id}' has no bespoke factory and no generic "
            f"transport builder for transport_type '{descriptor.transport_type}'."
        )
    return builder(config, descriptor)


class ProviderRegistry:
    """Cache and clean up provider instances by provider id."""

    def __init__(self, providers: MutableMapping[str, BaseProvider] | None = None):
        self._providers = providers if providers is not None else {}

    def is_cached(self, provider_id: str) -> bool:
        """Return whether a provider for this id is already in the cache."""
        return provider_id in self._providers

    def get(self, provider_id: str, settings: Settings) -> BaseProvider:
        if provider_id not in self._providers:
            self._providers[provider_id] = create_provider(provider_id, settings)
        return self._providers[provider_id]

    async def cleanup(self) -> None:
        """Call ``cleanup`` on every cached provider, then clear the cache.

        Attempts all providers even if one fails. A single failure is re-raised
        as-is; multiple failures are wrapped in :exc:`ExceptionGroup`.
        """
        items = list(self._providers.items())
        errors: list[Exception] = []
        try:
            for _pid, provider in items:
                try:
                    await provider.cleanup()
                except Exception as e:
                    errors.append(e)
        finally:
            self._providers.clear()
        if len(errors) == 1:
            raise errors[0]
        if len(errors) > 1:
            msg = "One or more provider cleanups failed"
            raise ExceptionGroup(msg, errors)
