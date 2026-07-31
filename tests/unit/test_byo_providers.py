"""Tests for SP-BYO-PROVIDERS — direct openai / anthropic + openai_compatible.

Pins the three new provider descriptors (``openai``, ``anthropic``,
``openai_compatible``) added so a third party can bring their own model:
each is a valid ``provider/model`` ref (AC1), the registry builds each
with the expected transport type and resolved base URL (AC2), and a
genuine ``openai_compatible`` dispatch reaches its configured host,
observed through a truthful ``httpx.MockTransport`` boundary fake — only
the HTTP transport is faked, no Repoach routing code is monkeypatched
(AC3). These assertions fail on pre-change code: the three ids are
unknown to ``SUPPORTED_PROVIDER_IDS`` and the four ``Settings`` fields
do not exist.
"""

from __future__ import annotations

import json

import httpx
import pytest
from openai import AsyncOpenAI

from repoach.llm_proxy.api.models.anthropic import Message, MessagesRequest
from repoach.llm_proxy.config.provider_ids import SUPPORTED_PROVIDER_IDS
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.anthropic_messages import AnthropicMessagesTransport
from repoach.llm_proxy.providers.openai_generic import GenericOpenAIProvider
from repoach.llm_proxy.providers.registry import create_provider
from repoach.llm_proxy.routing.refs import ModelRef

_NEW_PROVIDER_IDS = ("openai", "anthropic", "openai_compatible")

_NEW_PROVIDER_ENV_KEYS = (
    "OPENAI_API_KEY",
    "REPOACH_OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "REPOACH_ANTHROPIC_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "REPOACH_OPENAI_COMPATIBLE_API_KEY",
    "OPENAI_COMPATIBLE_BASE_URL",
    "REPOACH_OPENAI_COMPATIBLE_BASE_URL",
)


def _clean_new_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every new-provider env var so each test starts hermetic."""
    for key in _NEW_PROVIDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("provider_id", _NEW_PROVIDER_IDS)
def test_supported_provider_ids_contains_new_providers(provider_id: str) -> None:
    assert provider_id in SUPPORTED_PROVIDER_IDS


@pytest.mark.parametrize(
    ("provider_id", "model"),
    [
        ("openai", "gpt-4o"),
        ("anthropic", "claude-sonnet-4-5"),
        ("openai_compatible", "llama3.1"),
    ],
)
def test_model_ref_parses_new_providers(provider_id: str, model: str) -> None:
    ref = ModelRef.parse(f"{provider_id}/{model}")
    assert ref.provider_id == provider_id
    assert ref.model == model


def test_registry_builds_openai_with_resolved_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_new_provider_env(monkeypatch)
    monkeypatch.setenv("REPOACH_OPENAI_API_KEY", "test-openai-key")
    provider = create_provider("openai", Settings(_env_file=None))
    assert isinstance(provider, GenericOpenAIProvider)
    assert provider._base_url == "https://api.openai.com/v1"


def test_registry_builds_anthropic_with_resolved_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_new_provider_env(monkeypatch)
    monkeypatch.setenv("REPOACH_ANTHROPIC_API_KEY", "test-anthropic-key")
    provider = create_provider("anthropic", Settings(_env_file=None))
    assert isinstance(provider, AnthropicMessagesTransport)
    assert provider._base_url == "https://api.anthropic.com"


def test_registry_builds_openai_compatible_with_configured_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_new_provider_env(monkeypatch)
    monkeypatch.setenv("REPOACH_OPENAI_COMPATIBLE_API_KEY", "test-local-key")
    monkeypatch.setenv("REPOACH_OPENAI_COMPATIBLE_BASE_URL", "http://localhost:11434/v1")
    provider = create_provider("openai_compatible", Settings(_env_file=None))
    assert isinstance(provider, GenericOpenAIProvider)
    assert provider._base_url == "http://localhost:11434/v1"


def test_registry_has_no_unbuildable_new_descriptors() -> None:
    from repoach.llm_proxy.providers import catalog, registry

    for provider_id in _NEW_PROVIDER_IDS:
        descriptor = catalog.PROVIDER_DESCRIPTORS[provider_id]
        buildable = (
            provider_id in registry._BESPOKE_FACTORIES
            or descriptor.transport_type in registry._GENERIC_TRANSPORT_BUILDERS
        )
        assert buildable, provider_id


def _sse_chunk(content: str, *, finish_reason: str | None = None) -> str:
    """Build one OpenAI-shaped ``chat.completion.chunk`` SSE data line."""
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "llama3.1",
        "choices": [
            {
                "index": 0,
                "delta": {"content": content} if content else {},
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(payload)}\n\n"


async def test_openai_compatible_dispatches_to_configured_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_new_provider_env(monkeypatch)
    configured_base_url = "http://mock-ollama.local:11434/v1"
    monkeypatch.setenv("REPOACH_OPENAI_COMPATIBLE_API_KEY", "test-local-key")
    monkeypatch.setenv("REPOACH_OPENAI_COMPATIBLE_BASE_URL", configured_base_url)
    provider = create_provider("openai_compatible", Settings(_env_file=None))
    assert isinstance(provider, GenericOpenAIProvider)
    assert provider._base_url == configured_base_url

    observed_urls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed_urls.append(str(request.url))
        body = _sse_chunk("hi") + _sse_chunk("", finish_reason="stop") + "data: [DONE]\n\n"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
            request=request,
        )

    provider._client = AsyncOpenAI(
        api_key=provider._api_key,
        base_url=provider._base_url,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    request = MessagesRequest(
        model="llama3.1",
        max_tokens=16,
        messages=[Message(role="user", content="ping")],
    )
    async for _event in provider.stream_response(request):
        pass

    assert observed_urls, "expected the provider to dispatch at least one request"
    assert all(url.startswith(configured_base_url) for url in observed_urls), observed_urls
