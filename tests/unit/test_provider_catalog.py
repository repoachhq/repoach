"""Tests for the extensible provider catalog (SP-PROVIDER-CATALOG).

The catalog is the single source of provider identity: the supported id
set derives from the descriptor table, every descriptor has a factory,
and the legacy import paths re-export the same value.
"""

from __future__ import annotations

from repoach.llm_proxy.config.provider_ids import (
    SUPPORTED_PROVIDER_IDS as CONFIG_SUPPORTED,
)
from repoach.llm_proxy.providers import catalog, registry
from repoach.llm_proxy.providers.registry import (
    SUPPORTED_PROVIDER_IDS as REGISTRY_SUPPORTED,
)


def test_supported_ids_derive_from_descriptors() -> None:
    assert tuple(catalog.PROVIDER_DESCRIPTORS) == catalog.SUPPORTED_PROVIDER_IDS


def test_current_registry_is_the_ten_providers() -> None:
    assert catalog.SUPPORTED_PROVIDER_IDS == (
        "nvidia_nim",
        "open_router",
        "claude_code",
        "kimi",
        "groq",
        "cerebras",
        "deepseek",
        "openai",
        "anthropic",
        "openai_compatible",
    )


def test_every_descriptor_is_buildable() -> None:
    buildable = set(registry._BESPOKE_FACTORIES) | {
        provider_id
        for provider_id, descriptor in catalog.PROVIDER_DESCRIPTORS.items()
        if descriptor.transport_type in registry._GENERIC_TRANSPORT_BUILDERS
    }
    assert buildable == set(catalog.PROVIDER_DESCRIPTORS)


def test_descriptor_provider_ids_match_keys() -> None:
    for key, descriptor in catalog.PROVIDER_DESCRIPTORS.items():
        assert descriptor.provider_id == key


def test_legacy_import_paths_reexport_the_catalog() -> None:
    assert CONFIG_SUPPORTED is catalog.SUPPORTED_PROVIDER_IDS
    assert REGISTRY_SUPPORTED is catalog.SUPPORTED_PROVIDER_IDS
    assert registry.PROVIDER_DESCRIPTORS is catalog.PROVIDER_DESCRIPTORS


_LIVE_CHAIN_MODELS: tuple[str, ...] = (
    "nvidia_nim/z-ai/glm-5.2",
    "minimaxai/minimax-m3",
    "nvidia_nim/qwen/qwen3.7-max",
    "nvidia_nim/deepseek-ai/deepseek-v4-pro",
    "kimi/kimi-k2.6",
    "nvidia_nim/mistralai/mistral-medium-3.5",
    "claude_code/opus",
    "claude_code/sonnet",
    "claude_code/haiku",
)


def test_every_live_chain_model_has_a_thinking_class() -> None:
    for model_id in _LIVE_CHAIN_MODELS:
        assert catalog.classify_thinking(model_id) != "unknown", model_id


def test_classify_thinking_known_hybrid() -> None:
    assert catalog.classify_thinking("nvidia_nim/z-ai/glm-5.2") == "hybrid"
    assert catalog.classify_thinking("claude_code/opus") == "hybrid"


def test_classify_thinking_known_reasoner() -> None:
    assert catalog.classify_thinking("minimaxai/minimax-m3") == "reasoner"
    assert catalog.classify_thinking("kimi/kimi-k2.6") == "reasoner"


def test_classify_thinking_unknown_model() -> None:
    assert catalog.classify_thinking("nvidia_nim/some/unlisted-model") == "unknown"
    assert catalog.classify_thinking("") == "unknown"
