"""``doctor`` names the live provider route and its credential, never a value.

SP-PROVIDER-DIRECT-ANTHROPIC G4, delivered by SP-UNIVERSAL-PROVIDERS:
``claude_code`` and ``anthropic`` are routed in-process
(``provider_route=direct (<kind>)``), the gateway kinds print
``provider_route=gateway (<url>)`` or ``none``, and the still-unrouted
``openai`` / ``openai_compatible`` say so instead of implying the key is
enough.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repoach.cli.main import app
from repoach.core import config as core_config
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


def test_unrouted_kind_without_gateway(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", "openai_compatible")
    result = CliRunner().invoke(app, ["doctor"])
    assert "provider=openai_compatible (env; not routed)" in result.stdout
    assert "provider_route=none" in result.stdout
    assert "provider openai_compatible: not routed, set REPOACH_LLM_PROXY_BASE_URL" in result.stderr
    assert result.exit_code == 2


def test_unrouted_kind_with_gateway_names_the_live_route(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", "openai")
    _gateway_url(clone)
    result = CliRunner().invoke(app, ["doctor"])
    assert "provider_route=gateway (http://gateway.test.invalid:1)" in result.stdout
    assert "provider openai: not routed" in result.stderr


def test_gateway_kind(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", "gateway")
    result = CliRunner().invoke(app, ["doctor"])
    assert "provider=none" in result.stdout
    assert "provider_route=none" in result.stdout
    assert "REPOACH_PROVIDER=claude_code" in result.stderr
    assert "not routed" not in result.stderr
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
