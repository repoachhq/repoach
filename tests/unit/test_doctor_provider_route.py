"""``doctor`` names the live provider route and its credential, never a value.

SP-PROVIDER-DIRECT-ANTHROPIC G4, delivered by SP-UNIVERSAL-PROVIDERS:
``claude_code`` and ``anthropic`` are routed in-process
(``provider_route=direct (<kind>)``), the gateway kinds print
``provider_route=gateway (<url>)`` or ``none``; slice 2 routes
``openai_compatible``, its presets and ``gemini`` in-process too, and
lists every route with the credential names present
(``provider_option=``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repoach.cli.main import app
from repoach.core import config as core_config
from repoach.providers.errors import NO_PROVIDER_MESSAGE
from tests.provider_fakes import write_fake_claude
from tests.unit.test_provider_select import PROVIDER_ENV
from tests.unit.test_universal_install import _repo


@pytest.fixture
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))
    monkeypatch.delenv("REPOACH_DB_PATH", raising=False)
    monkeypatch.delenv("REPOACH_LLM_PROXY_BASE_URL", raising=False)
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(core_config, "_settings", None)
    repo = _repo(tmp_path / "work" / "clone")
    monkeypatch.chdir(repo)
    return tmp_path


def _gateway_url(clone: Path) -> None:
    user_env = clone / "xdg_config" / "repoach" / "env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text("REPOACH_LLM_PROXY_BASE_URL=http://gateway.test.invalid:1\n")


def test_anthropic_with_its_key_is_direct_even_beside_a_gateway(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC6: ``kind = anthropic`` and the key → ``provider_route=direct (anthropic)``."""
    monkeypatch.setenv("REPOACH_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "credential-value-two")
    _gateway_url(clone)
    result = CliRunner().invoke(app, ["doctor"])
    assert "provider=anthropic (env)" in result.stdout
    assert "provider_route=direct (anthropic)" in result.stdout
    assert "provider_credential=ANTHROPIC_API_KEY (present)" in result.stdout
    assert "repoach: provider" not in result.stderr
    assert "not routed" not in result.output


def test_anthropic_without_a_key_is_a_named_problem(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", "anthropic")
    result = CliRunner().invoke(app, ["doctor"])
    assert "provider_route=direct (anthropic)" in result.stdout
    assert "provider anthropic: no API key" in result.stderr
    assert result.exit_code == 2


def test_claude_code_logged_in(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli, _ = write_fake_claude(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{cli.parent}:{os.environ['PATH']}")
    monkeypatch.setenv("REPOACH_PROVIDER", "claude_code")
    result = CliRunner().invoke(app, ["doctor"])
    assert "provider=claude_code (env)" in result.stdout
    assert "provider_route=direct (claude_code)" in result.stdout
    assert f"provider_credential=claude login (claude.ai; {cli})" in result.stdout
    assert "example.invalid" not in result.output
    assert "repoach: provider" not in result.stderr


def test_claude_code_logged_out_or_absent(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli, _ = write_fake_claude(tmp_path / "bin", logged_in=False)
    monkeypatch.setenv("REPOACH_PROVIDER", "claude_code")
    monkeypatch.setenv("REPOACH_CLAUDE_CODE_CLI_PATH", str(cli))
    result = CliRunner().invoke(app, ["doctor"])
    assert f"provider_credential=none ({cli} not logged in)" in result.stdout
    assert "auth login" in result.stderr
    assert result.exit_code == 2

    monkeypatch.setenv("REPOACH_CLAUDE_CODE_CLI_PATH", str(tmp_path / "missing"))
    result = CliRunner().invoke(app, ["doctor"])
    assert "provider_credential=none (claude CLI unusable)" in result.stdout
    assert "claude CLI not found" in result.stderr
    assert result.exit_code == 2


def test_openai_compatible_without_base_url_is_a_named_problem(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", "openai_compatible")
    monkeypatch.setenv("REPOACH_MODEL_OPUS", "m")
    monkeypatch.setenv("REPOACH_MODEL_SONNET", "m")
    monkeypatch.setenv("REPOACH_MODEL_HAIKU", "m")
    result = CliRunner().invoke(app, ["doctor"])
    assert "provider=openai_compatible (env)" in result.stdout
    assert "provider_route=direct (openai_compatible)" in result.stdout
    assert "provider openai_compatible: no base URL" in result.stderr
    assert result.exit_code == 2


def _shim_forge_cli(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put a stand-in ``gh`` on PATH: ``doctor`` only checks the forge CLI exists.

    The CI image has no ``gh``; without it ``doctor`` reports a forge
    problem and exits 2 whatever the provider lines say.
    """
    bin_dir = clone / "forge_bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\nexit 0\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def test_preset_names_its_key_and_lists_every_route(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _shim_forge_cli(clone, monkeypatch)
    monkeypatch.setenv("REPOACH_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key-value")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key-value")
    for tier in ("OPUS", "SONNET", "HAIKU"):
        monkeypatch.setenv(f"REPOACH_MODEL_{tier}", "google/gemini-3.8-flash")
    result = CliRunner().invoke(app, ["doctor"])
    assert "provider=openrouter (env)" in result.stdout
    assert "provider_route=direct (openrouter)" in result.stdout
    assert "provider_credential=OPENROUTER_API_KEY (present)" in result.stdout
    options = [line for line in result.stdout.splitlines() if line.startswith("provider_option=")]
    assert [line.split(" ", 1)[0] for line in options] == [
        f"provider_option={name}"
        for name in (
            "claude_code",
            "anthropic",
            "openai_compatible",
            "gemini",
            "gateway",
            "openai",
            "ollama",
            "openrouter",
            "groq",
            "deepseek",
            "mistral",
        )
    ]
    gemini = next(line for line in options if line.startswith("provider_option=gemini "))
    assert "REPOACH_GEMINI_API_KEY=absent, GEMINI_API_KEY=present, GOOGLE_API_KEY=absent" in gemini
    ollama = next(line for line in options if line.startswith("provider_option=ollama "))
    assert "http://localhost:11434/v1; no key" in ollama
    assert "openrouter-key-value" not in result.output
    assert "gemini-key-value" not in result.output
    assert result.exit_code == 0, result.output


def test_nothing_configured_is_the_one_message(clone: Path) -> None:
    result = CliRunner().invoke(app, ["doctor"])
    assert "provider=none" in result.stdout
    assert f"repoach: {NO_PROVIDER_MESSAGE}" in result.stderr
    assert result.exit_code == 2


def test_gateway_kind(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", "gateway")
    result = CliRunner().invoke(app, ["doctor"])
    assert "provider=none" in result.stdout
    assert "provider_route=none" in result.stdout
    assert "provider gateway: REPOACH_LLM_PROXY_BASE_URL is not set" in result.stderr
    _gateway_url(clone)
    monkeypatch.setattr(core_config, "_settings", None)
    result = CliRunner().invoke(app, ["doctor"])
    assert "provider=gateway (http://gateway.test.invalid:1)" in result.stdout
    assert "provider_route=gateway (http://gateway.test.invalid:1)" in result.stdout
    assert "provider_credential=none (reads REPOACH_ANTHROPIC_AUTH_TOKEN)" in result.stdout


@pytest.mark.parametrize(
    ("present", "expected"),
    [
        (
            {
                "REPOACH_ANTHROPIC_API_KEY": "credential-value-one",
                "ANTHROPIC_API_KEY": "credential-value-two",
            },
            "REPOACH_ANTHROPIC_API_KEY (present)",
        ),
        ({"ANTHROPIC_API_KEY": "credential-value-two"}, "ANTHROPIC_API_KEY (present)"),
        ({}, "none (reads REPOACH_ANTHROPIC_API_KEY, then ANTHROPIC_API_KEY)"),
    ],
)
def test_anthropic_credential_order_never_prints_the_value(
    clone: Path, monkeypatch: pytest.MonkeyPatch, present: dict[str, str], expected: str
) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", "anthropic")
    for name, value in present.items():
        monkeypatch.setenv(name, value)
    result = CliRunner().invoke(app, ["doctor"])
    assert f"provider_credential={expected}" in result.stdout
    assert "credential-value-one" not in result.output
    assert "credential-value-two" not in result.output
