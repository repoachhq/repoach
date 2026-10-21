"""Provider selection: env > repoach.toml, gateway unchanged, named errors (SP-UNIVERSAL-PROVIDERS).

The gateway identity test pins the exact request the gateway route
sends: the body below was captured from ``origin/develop`` (d6335de)
with the same loop arguments, and this branch sends the same bytes.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from repoach.agent_engine.adapters import ProxyGatewayClient
from repoach.agent_engine.agent_loop import AgentLoop
from repoach.core import config as core_config
from repoach.core.config import GatewayConfigError
from repoach.llm.capability import CapabilityTier
from repoach.providers.direct import DirectProvider
from repoach.providers.errors import (
    NO_PROVIDER_MESSAGE,
    NoProviderConfiguredError,
    ProviderConfigError,
)
from repoach.providers.protocol import Provider
from repoach.providers.select import (
    build_direct_provider,
    require_provider_ready,
    resolve_route,
    tier_models,
)
from tests.provider_fakes import write_fake_claude
from tests.unit.test_universal_install import _repo

DEVELOP_GATEWAY_BODY = (
    '{"schema_version":"1","capability":"opus","system":"be terse","messages":[{"role":"user",'
    '"content":[{"type":"text","text":"Respond with ONLY a single JSON object — no prose, '
    'no markdown fences, no leading commentary.\\n\\nping"}]}],"tools":[],"max_tokens":321,'
    '"temperature":0.0,"skip_models":[],"thinking":{"enabled":true,"type":"enabled",'
    '"budget_tokens":1024}}'
)

PROVIDER_ENV = (
    "REPOACH_PROVIDER",
    "REPOACH_MODEL_OPUS",
    "REPOACH_MODEL_SONNET",
    "REPOACH_MODEL_HAIKU",
    "REPOACH_CLAUDE_CODE_CLI_PATH",
    "REPOACH_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
    "REPOACH_ANTHROPIC_BASE_URL",
    "REPOACH_ANTHROPIC_AUTH_TOKEN",
    "REPOACH_OPENAI_BASE_URL",
    "REPOACH_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "REPOACH_GEMINI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "REPOACH_OPENROUTER_API_KEY",
    "OPENROUTER_API_KEY",
    "REPOACH_GROQ_API_KEY",
    "GROQ_API_KEY",
    "REPOACH_DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY",
    "REPOACH_MISTRAL_API_KEY",
    "MISTRAL_API_KEY",
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh clone as cwd, with no provider variable from the developer's shell."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    repo = _repo(tmp_path / "clone")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(core_config, "_settings", None)
    return repo


def _toml(repo: Path, body: str) -> None:
    (repo / "repoach.toml").write_text(f"[repoach]\nschema = 1\n\n[provider]\n{body}\n")


def test_unset_kind_is_the_gateway(project: Path) -> None:
    assert resolve_route().kind == "gateway"


def test_env_outranks_the_file(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _toml(project, 'kind = "anthropic"')
    assert resolve_route().kind == "anthropic"
    assert resolve_route().source == "file"
    monkeypatch.setenv("REPOACH_PROVIDER", "claude_code")
    route = resolve_route()
    assert (route.kind, route.source) == ("claude_code", "env")


def test_openai_compatible_and_its_presets_route_in_process(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", "openai_compatible")
    route = resolve_route()
    assert (route.kind, route.configured, route.preset) == (
        "openai_compatible",
        "openai_compatible",
        "",
    )
    monkeypatch.setenv("REPOACH_PROVIDER", "ollama")
    route = resolve_route()
    assert (route.kind, route.preset, route.label) == ("openai_compatible", "ollama", "ollama")


def test_claude_code_is_never_chosen_implicitly(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G6: a ``claude`` binary on PATH and nothing named is the no-provider error, not a Max charge."""
    write_fake_claude(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    monkeypatch.delenv("REPOACH_LLM_PROXY_BASE_URL", raising=False)
    monkeypatch.setenv("REPOACH_ANTHROPIC_AUTH_TOKEN", "gateway-secret")
    monkeypatch.setattr(core_config, "_settings", None)
    with pytest.raises(NoProviderConfiguredError) as from_loop:
        AgentLoop(capability=CapabilityTier.SONNET)
    with pytest.raises(NoProviderConfiguredError) as from_preflight:
        require_provider_ready()
    assert str(from_loop.value) == str(from_preflight.value) == NO_PROVIDER_MESSAGE
    assert isinstance(from_loop.value, GatewayConfigError)


def test_tier_models_defaults_then_overrides(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", "claude_code")
    assert tier_models(resolve_route()) == {
        CapabilityTier.OPUS: "opus",
        CapabilityTier.SONNET: "sonnet",
        CapabilityTier.HAIKU: "haiku",
    }
    monkeypatch.setenv("REPOACH_PROVIDER", "anthropic")
    _toml(project, 'opus = "claude-opus-from-file"')
    monkeypatch.setenv("REPOACH_MODEL_HAIKU", "claude-haiku-from-env")
    assert tier_models(resolve_route()) == {
        CapabilityTier.OPUS: "claude-opus-from-file",
        CapabilityTier.SONNET: "claude-sonnet-5",
        CapabilityTier.HAIKU: "claude-haiku-from-env",
    }


def test_absent_cli_is_a_named_error(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", "claude_code")
    monkeypatch.setenv("REPOACH_CLAUDE_CODE_CLI_PATH", str(project / "no-such-claude"))
    with pytest.raises(ProviderConfigError, match="claude CLI not found") as info:
        require_provider_ready()
    assert "REPOACH_CLAUDE_CODE_CLI_PATH" in str(info.value)
    assert "\n" not in str(info.value)
    with pytest.raises(ProviderConfigError, match="claude CLI not found"):
        AgentLoop(capability=CapabilityTier.SONNET)


def test_logged_out_cli_is_a_named_error(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli, log = write_fake_claude(tmp_path / "bin", logged_in=False)
    monkeypatch.setenv("REPOACH_PROVIDER", "claude_code")
    monkeypatch.setenv("REPOACH_CLAUDE_CODE_CLI_PATH", str(cli))
    with pytest.raises(ProviderConfigError, match="is not logged in") as info:
        require_provider_ready()
    assert "auth login" in str(info.value)
    assert "example.invalid" not in str(info.value)
    assert not log.exists()


def test_logged_in_cli_is_ready_without_a_gateway(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli, _ = write_fake_claude(tmp_path / "bin")
    monkeypatch.setenv("REPOACH_PROVIDER", "claude_code")
    monkeypatch.setenv("REPOACH_CLAUDE_CODE_CLI_PATH", str(cli))
    monkeypatch.delenv("REPOACH_LLM_PROXY_BASE_URL", raising=False)
    monkeypatch.setattr(core_config, "_settings", None)
    assert require_provider_ready().kind == "claude_code"
    loop = AgentLoop(capability=CapabilityTier.OPUS)
    assert isinstance(loop._client, DirectProvider)
    assert isinstance(loop._client, Provider)
    assert loop._client.models[CapabilityTier.OPUS] == "opus"


def test_missing_anthropic_key_names_both_variables(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", "anthropic")
    with pytest.raises(ProviderConfigError) as info:
        require_provider_ready()
    message = str(info.value)
    assert "REPOACH_ANTHROPIC_API_KEY, then ANTHROPIC_API_KEY" in message
    with pytest.raises(ProviderConfigError):
        build_direct_provider(resolve_route(), timeout_s=5.0)


def test_anthropic_key_order(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "second-key")
    provider = build_direct_provider(resolve_route(), timeout_s=5.0)
    assert provider.transport_key[2] == "ANTHROPIC_API_KEY"
    monkeypatch.setenv("REPOACH_ANTHROPIC_API_KEY", "first-key")
    provider = build_direct_provider(resolve_route(), timeout_s=5.0)
    assert provider.transport_key[2] == "REPOACH_ANTHROPIC_API_KEY"


def test_gateway_route_request_is_byte_identical(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway route sends what ``develop`` sent: URL, auth headers and body bytes."""
    monkeypatch.setenv("REPOACH_LLM_PROXY_BASE_URL", "http://gateway.test.invalid:9")
    monkeypatch.setenv("REPOACH_ANTHROPIC_AUTH_TOKEN", "identity-secret")
    monkeypatch.setattr(core_config, "_settings", None)
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "schema_version": "1",
                "capability": "opus",
                "stop_reason": "end_turn",
                "model_used": "claude_code/opus",
                "content": [{"type": "text", "text": "pong"}],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                "elapsed_ms": 1,
                "trace": [],
            },
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    loop = AgentLoop(
        capability=CapabilityTier.OPUS,
        max_tokens=321,
        temperature=0.0,
        thinking={"type": "enabled", "budget_tokens": 1024},
    )
    assert isinstance(loop._client, ProxyGatewayClient)
    assert loop._client.name == "gateway"
    out = loop.run_oneshot("ping", system="be terse", json_response=True)

    assert out.text == "pong"
    (request,) = captured
    assert str(request.url) == "http://gateway.test.invalid:9/v1/agent"
    assert request.headers["authorization"] == "Bearer identity-secret"
    assert request.headers["x-api-key"] == "identity-secret"
    assert request.content.decode() == DEVELOP_GATEWAY_BODY
