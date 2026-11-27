"""Claude 5.5 models never receive ``temperature``, ``top_p`` or ``top_k`` (SP-PROVIDER-DIRECT-ANTHROPIC AC7).

Measured 2026-11-27 on api.anthropic.com: ``claude-opus-5-5``,
``claude-sonnet-5-5`` and ``claude-haiku-5-5`` answer HTTP 400
``invalid_request_error`` "`temperature` is deprecated for this model."
for any ``temperature`` value, and likewise for ``top_p`` and ``top_k``;
the same request without them answers 200. Every reviewer sends a
``temperature`` (the agent loop 0.1, the refuter 0.0), so in CI run
45986316427 each anthropic call took that 400, the failover peek saw no
content, and the breaker quarantined Anthropic for the whole job.

The boundary faked here is the Messages API only: a loopback HTTP server
(:class:`tests.provider_fakes.FakeAnthropicServer`) records the exact body
the transport posts. Two real paths reach it: the gateway (the reviewer's
``ProxyGatewayClient`` -> ``/v1/agent`` -> dispatcher -> failover service
-> the registry-built ``anthropic`` transport) and the in-process direct
provider (``REPOACH_PROVIDER=anthropic``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from repoach.agent_engine.adapters import ProxyGatewayClient
from repoach.agent_engine.agent_loop import AgentLoop
from repoach.core import config as core_config
from repoach.llm.capability import CapabilityTier
from repoach.llm_proxy.api.app import create_app
from repoach.llm_proxy.api.dependencies import get_settings
from repoach.llm_proxy.api.models.agent_v1 import Message, TextBlock
from repoach.llm_proxy.api.routes import get_proxy_service
from repoach.llm_proxy.api.services import ClaudeProxyService
from repoach.llm_proxy.config import settings as settings_module
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.base import BaseProvider, ProviderConfig
from repoach.llm_proxy.providers.catalog import PROVIDER_DESCRIPTORS
from repoach.llm_proxy.providers.open_router import OpenRouterProvider
from repoach.llm_proxy.providers.registry import _build_generic_anthropic
from repoach.llm_proxy.routing import reset_breaker
from tests.provider_fakes import FakeAnthropicServer
from tests.unit.test_provider_select import PROVIDER_ENV
from tests.unit.test_universal_install import _repo

SAMPLING = ("temperature", "top_p", "top_k")
MEASURED_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")
REVIEWER_TEMPERATURES = (0.0, 0.1)


@pytest.fixture(autouse=True)
def _fresh_breaker() -> Any:
    """Give every test an empty breaker so no earlier trip skips the candidate.

    Yields:
        Nothing; the breaker is reset before and after the test.
    """
    reset_breaker()
    yield
    reset_breaker()


class _OpenApp:
    """The ``httpx.Client`` the gateway client opens per call: the app, already started."""

    def __init__(self, client: TestClient) -> None:
        """Wrap the started app client.

        Args:
            client: The FastAPI test client serving the proxy app.
        """
        self._client = client

    def __enter__(self) -> TestClient:
        """Return the app client.

        Returns:
            The wrapped test client.
        """
        return self._client

    def __exit__(self, *_exc: object) -> None:
        """Leave the app running; the test owns its lifetime."""
        return None


def _gateway(
    monkeypatch: pytest.MonkeyPatch, chain: str, provider: BaseProvider
) -> ProxyGatewayClient:
    """Serve the real proxy app with ``chain`` as ``MODEL_SONNET`` and return the reviewer's client.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        chain: The ``MODEL_SONNET`` chain, one provider-prefixed ref.
        provider: The upstream transport the service resolves the ref to.

    Returns:
        The ``ProxyGatewayClient`` every reviewer uses, wired to the app.
    """
    monkeypatch.setenv("MODEL", chain)
    monkeypatch.setenv("MODEL_SONNET", chain)
    monkeypatch.setattr(settings_module, "_configured_env_files", lambda _cfg: ())
    settings = Settings(_env_file=None)
    service = ClaudeProxyService(
        settings=settings,
        provider_getter=lambda _provider_id: provider,
        token_counter=lambda *args, **kwargs: 0,
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_proxy_service] = lambda: service
    client = TestClient(app)
    monkeypatch.setattr(
        "repoach.agent_engine.adapters.httpx.Client", lambda **_kw: _OpenApp(client)
    )
    return ProxyGatewayClient(base_url="http://testserver", api_key="t", timeout_s=30.0)


def _anthropic(base_url: str) -> BaseProvider:
    """Build the ``anthropic`` upstream through the gateway registry's own builder.

    Args:
        base_url: The fake Messages API base URL.

    Returns:
        The transport the proxy builds for the ``anthropic`` descriptor.
    """
    return _build_generic_anthropic(
        ProviderConfig(api_key="test-key", base_url=base_url), PROVIDER_DESCRIPTORS["anthropic"]
    )


def _review_turn(gateway: ProxyGatewayClient, temperature: float) -> Any:
    """Send one reviewer-shaped turn: a system prompt, a diff, the reviewer's temperature.

    Args:
        gateway: The reviewer's gateway client.
        temperature: ``0.1`` (agent loop) or ``0.0`` (refuter).

    Returns:
        The gateway's ``AgentResponse``.
    """
    return gateway.call(
        capability=CapabilityTier.SONNET,
        system="You are the Architect reviewer.",
        messages=[Message(role="user", content=[TextBlock(text="review this diff")])],
        tools=[],
        max_tokens=4096,
        temperature=temperature,
    )


@pytest.mark.parametrize("model", MEASURED_MODELS)
@pytest.mark.parametrize("temperature", REVIEWER_TEMPERATURES)
def test_gateway_drops_sampling_for_claude_5_5(
    monkeypatch: pytest.MonkeyPatch, model: str, temperature: float
) -> None:
    """A reviewer turn through ``/v1/agent`` reaches api.anthropic.com without sampling fields."""
    with FakeAnthropicServer(text="ok") as server:
        gateway = _gateway(monkeypatch, f"anthropic/{model}", _anthropic(server.base_url))
        response = _review_turn(gateway, temperature)
    assert response.model_used.endswith(model)
    (request,) = server.requests
    assert request["path"] == "/v1/messages"
    assert request["body"]["model"] == model
    assert [name for name in SAMPLING if name in request["body"]] == []


def test_gateway_keeps_temperature_for_an_unlisted_anthropic_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An Anthropic model not measured as rejecting sampling still receives the temperature."""
    with FakeAnthropicServer(text="ok") as server:
        gateway = _gateway(monkeypatch, "anthropic/claude-sonnet-5", _anthropic(server.base_url))
        _review_turn(gateway, 0.1)
    (request,) = server.requests
    assert request["body"]["model"] == "claude-sonnet-5"
    assert request["body"]["temperature"] == 0.1


def test_gateway_keeps_temperature_for_another_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenRouter serving the same model id keeps the reviewer's temperature."""
    with FakeAnthropicServer(text="ok") as server:
        provider = OpenRouterProvider(ProviderConfig(api_key="test-key", base_url=server.base_url))
        gateway = _gateway(monkeypatch, "open_router/anthropic/claude-sonnet-5-5", provider)
        _review_turn(gateway, 0.0)
    (request,) = server.requests
    assert request["body"]["model"] == "anthropic/claude-sonnet-5-5"
    assert request["body"]["temperature"] == 0.0


def test_only_the_anthropic_descriptor_declares_fixed_sampling() -> None:
    """The capability is catalog data on ``anthropic`` alone; every other provider is unchanged."""
    assert PROVIDER_DESCRIPTORS["anthropic"].sampling_fixed_models == frozenset(MEASURED_MODELS)
    others = {
        descriptor.sampling_fixed_models
        for provider_id, descriptor in PROVIDER_DESCRIPTORS.items()
        if provider_id != "anthropic"
    }
    assert others == {frozenset()}


@pytest.fixture
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clone configured for the in-process ``anthropic`` provider and no gateway.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The clone's root, which is the working directory.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("REPOACH_LLM_PROXY_BASE_URL", raising=False)
    monkeypatch.setenv("REPOACH_PROVIDER", "anthropic")
    monkeypatch.setenv("REPOACH_ANTHROPIC_API_KEY", "test-key")
    repo = _repo(tmp_path / "clone")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(core_config, "_settings", None)
    return repo


@pytest.mark.parametrize("temperature", REVIEWER_TEMPERATURES)
def test_direct_provider_drops_sampling_for_claude_5_5(
    clone: Path, monkeypatch: pytest.MonkeyPatch, temperature: float
) -> None:
    """The in-process ``anthropic`` provider posts no sampling fields for a 5.5 model."""
    monkeypatch.setenv("REPOACH_MODEL_SONNET", "claude-sonnet-5-5")
    with FakeAnthropicServer(text="ok") as server:
        monkeypatch.setenv("REPOACH_ANTHROPIC_BASE_URL", server.base_url)
        out = AgentLoop(capability=CapabilityTier.SONNET, temperature=temperature).run_oneshot(
            "review this diff", system="You are the Architect reviewer."
        )
    assert out.text == "ok"
    (request,) = server.requests
    assert request["body"]["model"] == "claude-sonnet-5-5"
    assert [name for name in SAMPLING if name in request["body"]] == []


def test_direct_provider_keeps_temperature_for_an_unlisted_model(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-process provider's default model, not measured as rejecting it, keeps the temperature."""
    with FakeAnthropicServer(text="ok") as server:
        monkeypatch.setenv("REPOACH_ANTHROPIC_BASE_URL", server.base_url)
        AgentLoop(capability=CapabilityTier.SONNET, temperature=0.1).run_oneshot("review this diff")
    (request,) = server.requests
    assert request["body"]["model"] == "claude-sonnet-5"
    assert request["body"]["temperature"] == 0.1
