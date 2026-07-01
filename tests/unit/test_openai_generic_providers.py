"""Tests for the re-registered direct providers (SP-PROVIDER-REWIRE).

kimi / groq / cerebras / deepseek are registered as OpenAI-compatible
providers sharing :class:`GenericOpenAIProvider`. Verifies their catalog
descriptors, that the factory builds the shared transport with the probed
base URL, that the catalog unfreeze now lets :class:`ModelRef` accept
them, and that their credentials read from ``FEROVA_*_API_KEY``.
"""

from __future__ import annotations

import pytest

from ferova.llm_proxy.config.settings import Settings
from ferova.llm_proxy.providers import catalog
from ferova.llm_proxy.providers.openai_generic import GenericOpenAIProvider
from ferova.llm_proxy.providers.registry import create_provider
from ferova.llm_proxy.routing.refs import ModelRef

_PROVIDERS = [
    ("kimi", "FEROVA_KIMI_API_KEY", "https://api.moonshot.ai/v1"),
    ("groq", "FEROVA_GROQ_API_KEY", "https://api.groq.com/openai/v1"),
    ("cerebras", "FEROVA_CEREBRAS_API_KEY", "https://api.cerebras.ai/v1"),
    ("deepseek", "FEROVA_DEEPSEEK_API_KEY", "https://api.deepseek.com/v1"),
]


@pytest.mark.parametrize(("provider_id", "_env", "base_url"), _PROVIDERS)
def test_descriptor_registered(provider_id: str, _env: str, base_url: str) -> None:
    descriptor = catalog.PROVIDER_DESCRIPTORS[provider_id]
    assert descriptor.transport_type == "openai_chat"
    assert descriptor.default_base_url == base_url
    assert descriptor.credential_attr == f"{provider_id}_api_key"


@pytest.mark.parametrize(("provider_id", "key_env", "base_url"), _PROVIDERS)
def test_factory_builds_generic_transport(
    provider_id: str, key_env: str, base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(key_env, "test-key")
    provider = create_provider(provider_id, Settings(_env_file=None))
    assert isinstance(provider, GenericOpenAIProvider)
    assert provider._base_url == base_url


@pytest.mark.parametrize(("provider_id", "_env", "_base"), _PROVIDERS)
def test_modelref_now_accepts_provider(provider_id: str, _env: str, _base: str) -> None:
    ref = ModelRef.parse(f"{provider_id}/some-model")
    assert ref.provider_id == provider_id
    assert ref.model == "some-model"


@pytest.mark.parametrize(("provider_id", "key_env", "_base"), _PROVIDERS)
def test_credential_reads_from_sharp_env(
    provider_id: str, key_env: str, _base: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(key_env, "secret-value")
    settings = Settings(_env_file=None)
    assert getattr(settings, f"{provider_id}_api_key") == "secret-value"
