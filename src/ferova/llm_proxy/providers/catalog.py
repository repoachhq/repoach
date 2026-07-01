"""The provider catalog — the single source of provider identity.

This leaf module holds the descriptor table that defines every provider
the proxy knows about. :data:`SUPPORTED_PROVIDER_IDS` is **derived** from
it, so adding a provider is one descriptor here (plus a factory and a
client) — never a second hand-maintained id list or enum.

It lives apart from :mod:`providers.registry` (which wires factories and
needs :class:`Settings`) precisely so ``config`` and ``routing`` can
derive the id set without the registry's import cycle: it imports only
:mod:`providers.defaults`, and the ``providers`` package init pulls only
``base`` + ``exceptions``, none of which import ``config``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ferova.llm_proxy.providers.defaults import (
    CEREBRAS_DEFAULT_BASE,
    DEEPSEEK_DEFAULT_BASE,
    GROQ_DEFAULT_BASE,
    KIMI_DEFAULT_BASE,
    NVIDIA_NIM_DEFAULT_BASE,
    OPENROUTER_DEFAULT_BASE,
)

TransportType = Literal["openai_chat", "anthropic_messages"]


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """Metadata for building :class:`ProviderConfig` and factory wiring.

    Attributes:
        provider_id: Stable provider identifier (also the
            ``provider/`` prefix in model strings).
        transport_type: Wire-shape used by the upstream
            (``"openai_chat"`` / ``"anthropic_messages"``).
        capabilities: Tuple of capability tags exposed by the
            adapter (``"chat"``, ``"streaming"``, ``"tools"``, …).
        credential_env: Environment-variable name a CLI helper
            should print when credentials are missing.
        credential_url: Documentation URL printed alongside the env
            name to help the user obtain a key.
        credential_attr: When set, the API key is read from this
            attribute on :class:`Settings` (e.g.
            ``"nvidia_nim_api_key"``).
        static_credential: When set, the adapter uses this fixed
            literal instead of an env-driven key — used by local
            providers (LM Studio, llama.cpp).
        default_base_url: Built-in base URL used when no override is
            configured.
        base_url_attr: When set, the base URL is read from this
            :class:`Settings` attribute instead of the default.
        proxy_attr: When set, the HTTP proxy URL is read from this
            :class:`Settings` attribute.
    """

    provider_id: str
    transport_type: TransportType
    capabilities: tuple[str, ...]
    credential_env: str | None = None
    credential_url: str | None = None
    credential_attr: str | None = None
    static_credential: str | None = None
    default_base_url: str | None = None
    base_url_attr: str | None = None
    proxy_attr: str | None = None


PROVIDER_DESCRIPTORS: dict[str, ProviderDescriptor] = {
    "nvidia_nim": ProviderDescriptor(
        provider_id="nvidia_nim",
        transport_type="openai_chat",
        credential_env="NVIDIA_NIM_API_KEY",
        credential_url="https://build.nvidia.com/settings/api-keys",
        credential_attr="nvidia_nim_api_key",
        default_base_url=NVIDIA_NIM_DEFAULT_BASE,
        proxy_attr="nvidia_nim_proxy",
        capabilities=("chat", "streaming", "tools", "thinking", "rate_limit"),
    ),
    "open_router": ProviderDescriptor(
        provider_id="open_router",
        transport_type="anthropic_messages",
        credential_env="OPENROUTER_API_KEY",
        credential_url="https://openrouter.ai/keys",
        credential_attr="open_router_api_key",
        default_base_url=OPENROUTER_DEFAULT_BASE,
        proxy_attr="open_router_proxy",
        capabilities=("chat", "streaming", "tools", "thinking", "native_anthropic"),
    ),
    "claude_code": ProviderDescriptor(
        provider_id="claude_code",
        transport_type="anthropic_messages",
        static_credential="claude-code-oauth",
        capabilities=("chat", "tools", "native_anthropic", "subscription"),
    ),
    "kimi": ProviderDescriptor(
        provider_id="kimi",
        transport_type="openai_chat",
        credential_env="KIMI_API_KEY",
        credential_url="https://platform.moonshot.ai/console/api-keys",
        credential_attr="kimi_api_key",
        default_base_url=KIMI_DEFAULT_BASE,
        capabilities=("chat", "streaming", "tools"),
    ),
    "groq": ProviderDescriptor(
        provider_id="groq",
        transport_type="openai_chat",
        credential_env="GROQ_API_KEY",
        credential_url="https://console.groq.com/keys",
        credential_attr="groq_api_key",
        default_base_url=GROQ_DEFAULT_BASE,
        capabilities=("chat", "streaming", "tools"),
    ),
    "cerebras": ProviderDescriptor(
        provider_id="cerebras",
        transport_type="openai_chat",
        credential_env="CEREBRAS_API_KEY",
        credential_url="https://cloud.cerebras.ai",
        credential_attr="cerebras_api_key",
        default_base_url=CEREBRAS_DEFAULT_BASE,
        capabilities=("chat", "streaming", "tools"),
    ),
    "deepseek": ProviderDescriptor(
        provider_id="deepseek",
        transport_type="openai_chat",
        credential_env="DEEPSEEK_API_KEY",
        credential_url="https://platform.deepseek.com/api_keys",
        credential_attr="deepseek_api_key",
        default_base_url=DEEPSEEK_DEFAULT_BASE,
        capabilities=("chat", "streaming", "tools"),
    ),
}


SUPPORTED_PROVIDER_IDS: tuple[str, ...] = tuple(PROVIDER_DESCRIPTORS)
