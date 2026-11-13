"""Tests for SP-DIRECT-ANTIGRAVITY-ROUTE — direct in-process antigravity route and doctor preflight."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repoach.agent_engine.agent_loop import AgentLoop
from repoach.cli.main import app
from repoach.core import config as core_config
from repoach.llm.capability import CapabilityTier
from repoach.llm_proxy.api.models.agent_v1 import Message, TextBlock
from repoach.providers.errors import (
    GatewayTransportError,
    ProviderAuthError,
    ProviderConfigError,
)
from repoach.providers.prices import SUBSCRIPTION_PROVIDERS
from repoach.providers.select import (
    antigravity_cli_env,
    antigravity_version,
    build_direct_provider,
    require_provider_ready,
    resolve_route,
    tier_models,
)
from tests.provider_fakes import read_calls, write_fake_agy
from tests.unit.test_provider_select import PROVIDER_ENV
from tests.unit.test_universal_install import _repo

MEMORY_SWITCHES = (
    "REPOACH_REVIEW_MEMORY_ENABLED",
    "REPOACH_BUILDER_MEMORY_ENABLED",
    "REPOACH_REVIEW_LESSONS_ENABLED",
)


@pytest.fixture
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a fresh repository clone with REPOACH_PROVIDER=antigravity."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))
    monkeypatch.setenv("REPOACH_DB_PATH", str(tmp_path / "review.db"))
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("REPOACH_LLM_PROXY_BASE_URL", raising=False)
    for name in MEMORY_SWITCHES:
        monkeypatch.setenv(name, "false")
    monkeypatch.setenv("REPOACH_PROVIDER", "antigravity")
    repo = _repo(tmp_path / "clone")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(core_config, "_settings", None)
    return repo


def test_resolve_route_and_default_tier_models_for_antigravity(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: REPOACH_PROVIDER=antigravity resolves route.kind and default tier models."""
    route = resolve_route(clone)
    assert route.kind == "antigravity"
    assert route.label == "antigravity"
    assert "antigravity" in SUBSCRIPTION_PROVIDERS
    assert tier_models(route) == {
        CapabilityTier.OPUS: "gemini-4-argon-high",
        CapabilityTier.SONNET: "gemini-4-argon-medium",
        CapabilityTier.HAIKU: "gemini-3.8-flash-medium",
    }

    monkeypatch.setenv("REPOACH_MODEL_SONNET", "claude-sonnet-5-5-medium")
    overridden = resolve_route(clone)
    assert tier_models(overridden)[CapabilityTier.SONNET] == "claude-sonnet-5-5-medium"


def test_antigravity_cli_env_strips_foreign_keys_and_keeps_gemini_api_key() -> None:
    """AC2: antigravity_cli_env drops foreign keys/tokens and preserves GEMINI_API_KEY."""
    raw_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp/home",
        "ANTHROPIC_API_KEY": "anth-secret",
        "OPENAI_API_KEY": "openai-secret",
        "REPOACH_ANTHROPIC_AUTH_TOKEN": "gateway-token",
        "GITHUB_TOKEN": "gh-token",
        "GEMINI_API_KEY": "gemini-kept-key",
        "AGY_ADC_AUTH": "true",
    }
    cleaned = antigravity_cli_env(raw_env)
    assert "ANTHROPIC_API_KEY" not in cleaned
    assert "OPENAI_API_KEY" not in cleaned
    assert "REPOACH_ANTHROPIC_AUTH_TOKEN" not in cleaned
    assert "GITHUB_TOKEN" not in cleaned
    assert cleaned["GEMINI_API_KEY"] == "gemini-kept-key"
    assert cleaned["PATH"] == "/usr/bin:/bin"
    assert cleaned["AGY_ADC_AUTH"] == "true"


def test_direct_antigravity_provider_end_to_end_with_fake_agy(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: DirectProvider.call runs fake agy and maps authentication failure to ProviderAuthError."""
    cli, log = write_fake_agy(tmp_path / "bin", result="pong from agy")
    monkeypatch.setenv("PATH", f"{cli.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("REPOACH_CLAUDE_CODE_CLI_PATH", str(tmp_path / "wrong-claude-binary"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("GEMINI_API_KEY", "allowed-gemini-key")

    ready = require_provider_ready(clone)
    assert ready.kind == "antigravity"

    out = AgentLoop(capability=CapabilityTier.SONNET).run_oneshot("ping", system="be terse")
    assert out.text == "pong from agy"
    assert out.model_used == "antigravity/gemini-4-argon-medium"

    (call,) = read_calls(log)
    argv = call["argv"]
    assert argv[argv.index("--model") + 1] == "gemini-4-argon-medium"
    assert "ANTHROPIC_API_KEY" not in call["env"]
    assert call["env"].get("GEMINI_API_KEY") == "allowed-gemini-key"
    assert call["session_leader"] is True

    direct = build_direct_provider(resolve_route(clone), timeout_s=5.0)
    reply = direct.call(
        capability=CapabilityTier.SONNET,
        system="be terse",
        messages=[Message(role="user", content=[TextBlock(text="ping")])],
        tools=[],
        max_tokens=64,
        temperature=0.0,
    )
    assert reply.content[0].text == "pong from agy"
    assert reply.usage.input_tokens == 11
    assert reply.usage.cache_read_tokens == 100
    assert reply.usage.output_tokens == 7

    bad_cli, _ = write_fake_agy(
        tmp_path / "bad_bin",
        exit_code=1,
        error_text="authentication required: please sign in",
    )
    monkeypatch.setenv("REPOACH_ANTIGRAVITY_CLI_PATH", str(bad_cli))
    provider = build_direct_provider(resolve_route(clone), timeout_s=5.0)
    with pytest.raises(ProviderAuthError) as auth_exc:
        provider.call(
            capability=CapabilityTier.SONNET,
            system="",
            messages=[Message(role="user", content=[TextBlock(text="hi")])],
            tools=[],
            max_tokens=64,
            temperature=0.0,
        )
    assert not isinstance(auth_exc.value, GatewayTransportError)
    assert "sign in" in str(auth_exc.value)


def test_doctor_reports_antigravity_route_and_binary(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: repoach doctor reports direct (antigravity), agy binary, and provider_option=antigravity."""
    cli, _ = write_fake_agy(tmp_path / "bin", version="1.5.3")
    gh = tmp_path / "bin" / "gh"
    gh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{cli.parent}{os.pathsep}{os.environ['PATH']}")

    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert f"agy={cli}" in result.stdout
    assert "provider=antigravity (env)" in result.stdout
    assert "provider_route=direct (antigravity)" in result.stdout
    assert f"provider_credential=agy CLI (1.5.3; {cli})" in result.stdout
    assert (
        f"provider_option=antigravity (agy CLI at {cli}; login or GEMINI_API_KEY)" in result.stdout
    )

    monkeypatch.setenv("REPOACH_ANTIGRAVITY_CLI_PATH", str(tmp_path / "no-such-agy"))
    with pytest.raises(ProviderConfigError, match="agy CLI not found"):
        require_provider_ready(clone)
    bad_doctor = CliRunner().invoke(app, ["doctor"])
    assert bad_doctor.exit_code == 2
    assert "provider_credential=none (agy CLI unusable)" in bad_doctor.stdout
    assert "agy CLI not found" in bad_doctor.stderr


def test_antigravity_version_nonzero_exit_raises_provider_config_error(tmp_path: Path) -> None:
    """antigravity_version raises ProviderConfigError when agy --version exits non-zero or empty."""
    failing_cli = tmp_path / "agy-fail"
    failing_cli.write_text("#!/bin/sh\necho 'broken binary' >&2\nexit 2\n", encoding="utf-8")
    failing_cli.chmod(0o755)
    with pytest.raises(ProviderConfigError, match="--version' failed"):
        antigravity_version(str(failing_cli))

    empty_cli = tmp_path / "agy-empty"
    empty_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    empty_cli.chmod(0o755)
    with pytest.raises(ProviderConfigError, match="--version' failed"):
        antigravity_version(str(empty_cli))
