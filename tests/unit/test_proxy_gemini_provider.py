"""Tests for SP-PROXY-GEMINI-PROVIDER — Gemini API in the chain-failover gateway."""

from __future__ import annotations

import pytest

from repoach.llm_proxy.config.provider_ids import SUPPORTED_PROVIDER_IDS
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.catalog import classify_thinking
from repoach.llm_proxy.providers.defaults import GEMINI_DEFAULT_BASE
from repoach.llm_proxy.providers.exceptions import AuthenticationError
from repoach.llm_proxy.providers.openai_generic import GenericOpenAIProvider
from repoach.llm_proxy.providers.registry import create_provider
from repoach.llm_proxy.routing.chain import Chain
from repoach.providers import select as provider_select

_GEMINI_ENV_KEYS = (
    "REPOACH_GEMINI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "REPOACH_GEMINI_BASE_URL",
    "GEMINI_BASE_URL",
)


def _clean_gemini_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip Gemini credential and base URL variables from the environment."""
    for key in _GEMINI_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_gemini_descriptor_registered_and_buildable(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1: gemini is registered in SUPPORTED_PROVIDER_IDS and builds a GenericOpenAIProvider."""
    _clean_gemini_env(monkeypatch)
    monkeypatch.setenv("REPOACH_GEMINI_API_KEY", "test-gemini-key")
    assert "gemini" in SUPPORTED_PROVIDER_IDS
    assert provider_select.GEMINI_DEFAULT_BASE is GEMINI_DEFAULT_BASE
    provider = create_provider("gemini", Settings(_env_file=None))
    assert isinstance(provider, GenericOpenAIProvider)
    assert provider._base_url == "https://generativelanguage.googleapis.com/v1beta/openai"

    monkeypatch.setenv("REPOACH_GEMINI_BASE_URL", "http://127.0.0.1:9009/v1beta/openai")
    custom_provider = create_provider("gemini", Settings(_env_file=None))
    assert isinstance(custom_provider, GenericOpenAIProvider)
    assert custom_provider._base_url == "http://127.0.0.1:9009/v1beta/openai"


def test_gemini_api_key_precedence_repoach_then_gemini_then_google(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2: Settings resolves REPOACH_GEMINI_API_KEY > GEMINI_API_KEY > GOOGLE_API_KEY."""
    _clean_gemini_env(monkeypatch)
    monkeypatch.setenv("GOOGLE_API_KEY", "from-google")
    assert Settings(_env_file=None).gemini_api_key == "from-google"

    monkeypatch.setenv("GEMINI_API_KEY", "from-gemini")
    assert Settings(_env_file=None).gemini_api_key == "from-gemini"

    monkeypatch.setenv("REPOACH_GEMINI_API_KEY", "from-repoach")
    assert Settings(_env_file=None).gemini_api_key == "from-repoach"


def test_gemini_missing_key_raises_authentication_error_with_aistudio_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: Missing key raises AuthenticationError naming GEMINI_API_KEY and AI Studio URL."""
    _clean_gemini_env(monkeypatch)
    with pytest.raises(AuthenticationError) as exc_info:
        create_provider("gemini", Settings(_env_file=None, gemini_api_key=""))
    message = str(exc_info.value)
    assert "GEMINI_API_KEY" in message
    assert "https://aistudio.google.com/app/api-keys" in message


def test_gemini_chain_parses_and_classifies_thinking() -> None:
    """AC4: Chain.parse accepts gemini/* refs and classify_thinking returns hybrid."""
    chain = Chain.parse("gemini/gemini-2.5-pro,gemini/gemini-3.8-flash,gemini/gemini-4-argon-high")
    assert [(ref.provider_id, ref.model) for ref in chain.refs] == [
        ("gemini", "gemini-2.5-pro"),
        ("gemini", "gemini-3.8-flash"),
        ("gemini", "gemini-4-argon-high"),
    ]
    assert classify_thinking("gemini/gemini-2.5-pro") == "hybrid"
    assert classify_thinking("gemini/gemini-3.8-flash") == "hybrid"
    assert classify_thinking("gemini/gemini-4-argon-high") == "hybrid"
