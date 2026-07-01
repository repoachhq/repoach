"""Tests for the extensible provider catalog (SP-PROVIDER-CATALOG).

The catalog is the single source of provider identity: the supported id
set derives from the descriptor table, every descriptor has a factory,
and the legacy import paths re-export the same value.
"""

from __future__ import annotations

from ferova.llm_proxy.config.provider_ids import (
    SUPPORTED_PROVIDER_IDS as CONFIG_SUPPORTED,
)
from ferova.llm_proxy.providers import catalog, registry
from ferova.llm_proxy.providers.registry import (
    SUPPORTED_PROVIDER_IDS as REGISTRY_SUPPORTED,
)


def test_supported_ids_derive_from_descriptors() -> None:
    assert tuple(catalog.PROVIDER_DESCRIPTORS) == catalog.SUPPORTED_PROVIDER_IDS


def test_current_registry_is_the_seven_providers() -> None:
    assert catalog.SUPPORTED_PROVIDER_IDS == (
        "nvidia_nim",
        "open_router",
        "claude_code",
        "kimi",
        "groq",
        "cerebras",
        "deepseek",
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
