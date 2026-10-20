"""The ``claude_code`` provider: the logged-in CLI, in-process, no gateway (SP-UNIVERSAL-PROVIDERS).

The model boundary is a fake ``claude`` executable
(:func:`tests.provider_fakes.write_fake_claude`): the real
:class:`~repoach.llm_proxy.providers.claude_code.ClaudeCodeProvider`
spawns it exactly as it spawns the real CLI.  The forge boundary is the
stubbed-transport GitHub forge the review-team tests already use.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from repoach.agent_engine.adapters import GatewayError, GatewayTransportError
from repoach.agent_engine.agent_loop import AgentLoop
from repoach.cli.review_cmds import review_app
from repoach.core import config as core_config
from repoach.llm.capability import CapabilityTier
from tests.provider_fakes import APPROVE_JSON, read_calls, write_fake_claude
from tests.unit.test_provider_select import PROVIDER_ENV
from tests.unit.test_review_team import _StubGitHubForge
from tests.unit.test_universal_install import _repo

MEMORY_SWITCHES = (
    "REPOACH_REVIEW_MEMORY_ENABLED",
    "REPOACH_BUILDER_MEMORY_ENABLED",
    "REPOACH_REVIEW_LESSONS_ENABLED",
)


@pytest.fixture
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clone as cwd, ``REPOACH_PROVIDER=claude_code``, no gateway anywhere."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))
    monkeypatch.setenv("REPOACH_DB_PATH", str(tmp_path / "review.db"))
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("REPOACH_LLM_PROXY_BASE_URL", raising=False)
    for name in MEMORY_SWITCHES:
        monkeypatch.setenv(name, "false")
    monkeypatch.setenv("REPOACH_PROVIDER", "claude_code")
    repo = _repo(tmp_path / "clone")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(core_config, "_settings", None)
    return repo


def _on_path(monkeypatch: pytest.MonkeyPatch, cli: Path) -> None:
    monkeypatch.setenv("PATH", f"{cli.parent}:{os.environ['PATH']}")


def test_oneshot_runs_the_cli_with_the_tier_model(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli, log = write_fake_claude(tmp_path / "bin", result="pong")
    _on_path(monkeypatch, cli)
    out = AgentLoop(capability=CapabilityTier.OPUS).run_oneshot("ping", system="be terse")

    assert out.text == "pong"
    assert out.model_used == "claude_code/opus"
    (call,) = read_calls(log)
    argv = call["argv"]
    assert argv[argv.index("--model") + 1] == "opus"
    assert "--print" in argv and "--system-prompt-file" in argv
    assert call["system_chars"] == len("be terse")
    assert call["session_leader"] is True
    assert Path(call["cwd"]).resolve() != clone.resolve()


def test_haiku_tier_maps_to_the_haiku_alias(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli, log = write_fake_claude(tmp_path / "bin", result="ok")
    monkeypatch.setenv("REPOACH_CLAUDE_CODE_CLI_PATH", str(cli))
    AgentLoop(capability=CapabilityTier.HAIKU).run_oneshot("x")
    argv = read_calls(log)[0]["argv"]
    assert argv[argv.index("--model") + 1] == "haiku"


def test_expired_login_is_not_retried(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli, log = write_fake_claude(
        tmp_path / "bin", exit_code=1, stderr="Invalid API key · Please run /login"
    )
    _on_path(monkeypatch, cli)
    loop = AgentLoop(capability=CapabilityTier.SONNET)
    with pytest.raises(GatewayError) as info:
        loop.run_oneshot("x")
    assert not isinstance(info.value, GatewayTransportError)
    assert "auth login" in str(info.value)
    assert len(read_calls(log)) == 1


def test_other_cli_failure_is_a_transport_error(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli, _ = write_fake_claude(tmp_path / "bin", exit_code=2, stderr="overloaded, try later")
    _on_path(monkeypatch, cli)
    with pytest.raises(GatewayTransportError, match="overloaded"):
        AgentLoop(capability=CapabilityTier.SONNET).run_oneshot("x")


def test_timeout_kills_the_cli_and_is_a_transport_error(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli, log = write_fake_claude(tmp_path / "bin", sleep_s=30.0)
    _on_path(monkeypatch, cli)
    loop = AgentLoop(capability=CapabilityTier.SONNET, per_call_timeout_s=1.0)
    with pytest.raises(GatewayTransportError, match="timed out"):
        loop.run_oneshot("x")
    (call,) = read_calls(log)
    with pytest.raises(ProcessLookupError):
        os.killpg(call["pid"], 0)


class _CountingForge(_StubGitHubForge):
    """The stub forge, recording every transport call it receives."""

    def __init__(self) -> None:
        super().__init__()
        self.transport_calls: list[str] = []

    def _run(self, args: list[str], *, input_data: str | None = None) -> Any:
        self.transport_calls.append(" ".join(args))
        return super()._run(args, input_data=input_data)

    def _pr_diff(self, pr_number: int) -> Any:
        self.transport_calls.append(f"pr diff {pr_number}")
        return super()._pr_diff(pr_number)


def _review(monkeypatch: pytest.MonkeyPatch, forge: _StubGitHubForge) -> Any:
    selected: list[str] = []

    def select(**kwargs: Any) -> _StubGitHubForge:
        selected.append(str(kwargs.get("override")))
        return forge

    monkeypatch.setattr("repoach.cli.review_cmds.select_forge", select)
    monkeypatch.setattr("repoach.review.orchestrator.select_forge", select)
    result = CliRunner().invoke(review_app, ["pr", "42", "--dry-run", "--forge", "github"])
    return result, selected


def test_review_pr_dry_run_end_to_end_without_a_gateway(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``[provider] kind = "claude_code"`` and a logged-in CLI: four reviews, nothing posted."""
    (clone / "repoach.toml").write_text(
        '[repoach]\nschema = 1\n\n[provider]\nkind = "claude_code"\n'
    )
    monkeypatch.delenv("REPOACH_PROVIDER")
    cli, log = write_fake_claude(tmp_path / "bin", result=APPROVE_JSON)
    _on_path(monkeypatch, cli)
    forge = _CountingForge()

    result, _ = _review(monkeypatch, forge)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["final_verdict"] == "APPROVE"
    assert len(payload["reviews"]) == 4
    assert {review["verdict"] for review in payload["reviews"]} == {"APPROVE"}
    assert (payload["posted_comments"], payload["posted_reviews"]) == (0, 0)
    assert forge.posted_reviews == [] and forge.posted_comments == []
    assert "pr diff 42" in forge.transport_calls
    calls = read_calls(log)
    assert len(calls) >= 4
    assert {c["argv"][c["argv"].index("--model") + 1] for c in calls} <= {"opus", "sonnet", "haiku"}


@pytest.mark.parametrize(
    ("logged_in", "cli_path", "expected"),
    [
        (True, "no-such-claude", "claude CLI not found"),
        (False, None, "is not logged in"),
    ],
)
def test_unusable_cli_fails_before_any_forge_call(
    clone: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    logged_in: bool,
    cli_path: str | None,
    expected: str,
) -> None:
    cli, log = write_fake_claude(tmp_path / "bin", logged_in=logged_in)
    monkeypatch.setenv(
        "REPOACH_CLAUDE_CODE_CLI_PATH", str(tmp_path / cli_path) if cli_path else str(cli)
    )
    forge = _CountingForge()

    result, selected = _review(monkeypatch, forge)

    assert result.exit_code == 2
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert len(lines) == 1 and expected in lines[0]
    assert selected in ([], ["github"])
    assert forge.transport_calls == []
    assert forge.posted_reviews == [] and forge.archive_calls == []
    assert read_calls(log) == []


def test_the_cli_never_inherits_a_key_or_a_token(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key beside ``kind = claude_code`` must not reach ``claude --print`` (it would bill the API)."""
    cli, log = write_fake_claude(tmp_path / "bin", result="ok")
    monkeypatch.setenv("REPOACH_CLAUDE_CODE_CLI_PATH", str(cli))
    secrets = {
        "ANTHROPIC_API_KEY": "leak-one",
        "REPOACH_ANTHROPIC_API_KEY": "leak-two",
        "REPOACH_ANTHROPIC_AUTH_TOKEN": "leak-three",
        "GITLAB_TOKEN": "leak-four",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "kept-login")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("LANG", "C.UTF-8")

    AgentLoop(capability=CapabilityTier.SONNET).run_oneshot("x")

    env = read_calls(log)[0]["env"]
    assert not set(secrets) & set(env)
    assert not any(value in env.values() for value in secrets.values())
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "kept-login"
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "claude-config")
    assert env["LANG"] == "C.UTF-8"
    assert env["HOME"] == os.environ["HOME"]
    assert env["PATH"] == os.environ["PATH"]
