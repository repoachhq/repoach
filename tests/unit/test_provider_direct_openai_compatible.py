"""``openai_compatible``, its presets and ``gemini``: chat completions in-process, no gateway.

SP-UNIVERSAL-PROVIDERS slice 2.  The model boundary is a real HTTP
server on the loopback (:class:`tests.provider_fakes.FakeChatServer`)
reached through ``REPOACH_OPENAI_BASE_URL`` (bare ``openai_compatible``) or
through the preset's / Gemini's own fixed host pointed at the fake; the gateway's own
:class:`~repoach.llm_proxy.providers.openai_generic.GenericOpenAIProvider`
speaks to it exactly as it speaks to OpenAI, Ollama, OpenRouter or
Gemini's OpenAI-compatible surface.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from repoach.agent_engine.adapters import GatewayError, GatewayTransportError
from repoach.agent_engine.agent_loop import AgentLoop
from repoach.cli.main import app as main_app
from repoach.cli.review_cmds import review_app
from repoach.core import config as core_config
from repoach.llm.capability import CapabilityTier
from repoach.providers import select as select_module
from repoach.providers.errors import (
    NO_PROVIDER_MESSAGE,
    ProviderAuthError,
    ProviderConfigError,
    ProviderRequestError,
)
from repoach.providers.select import (
    KEYLESS_API_KEY,
    PRESETS,
    SELECTABLE,
    Preset,
    openai_compatible_base_url,
    resolve_route,
    route_base_url,
    set_provider_override,
)
from repoach.providers.usage import read_rows
from tests.provider_fakes import APPROVE_JSON, FakeChatServer
from tests.unit.test_provider_direct_claude_code import MEMORY_SWITCHES, _CountingForge, _review
from tests.unit.test_provider_select import PROVIDER_ENV
from tests.unit.test_universal_install import _repo

TIERS = ("OPUS", "SONNET", "HAIKU")


@pytest.fixture
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clone as cwd with no provider, no gateway and a ledger under ``tmp_path``."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))
    monkeypatch.setenv("REPOACH_DB_PATH", str(tmp_path / "review.db"))
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("REPOACH_LLM_PROXY_BASE_URL", raising=False)
    for name in MEMORY_SWITCHES:
        monkeypatch.setenv(name, "false")
    repo = _repo(tmp_path / "clone")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(core_config, "_settings", None)
    return repo


def _models(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    for tier in TIERS:
        monkeypatch.setenv(f"REPOACH_MODEL_{tier}", model)


def _point_at(monkeypatch: pytest.MonkeyPatch, name: str, url: str) -> None:
    """Give preset ``name`` (or ``gemini``) the fake server as its fixed host."""
    if name == "gemini":
        monkeypatch.setattr(select_module, "GEMINI_DEFAULT_BASE", url)
        return
    keys = PRESETS[name].credential_names
    monkeypatch.setitem(PRESETS, name, Preset(base_url=url, credential_names=keys))


def _rows(tmp_path: Path) -> list[Any]:
    return read_rows(tmp_path / "review.db")


def test_ollama_preset_takes_no_key_and_reports_upstream_usage(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ollama-shaped stream, root without ``/v1``: normalised, no key sent, usage recorded at cost 0."""
    monkeypatch.setenv("REPOACH_PROVIDER", "ollama")
    monkeypatch.setenv("REPOACH_MODEL_SONNET", "llama3.1:8b")
    monkeypatch.setenv("REPOACH_MODEL_OPUS", "llama3.1:8b")
    monkeypatch.setenv("REPOACH_MODEL_HAIKU", "llama3.2:1b")
    with FakeChatServer(text="pong", shape="ollama") as server:
        _point_at(monkeypatch, "ollama", server.root)
        out = AgentLoop(capability=CapabilityTier.SONNET, max_tokens=77).run_oneshot(
            "ping", system="be terse"
        )
    assert out.text == "pong"
    assert out.model_used == "ollama/llama3.1:8b"
    (request,) = server.requests
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["authorization"] == f"Bearer {KEYLESS_API_KEY}"
    assert request["body"]["model"] == "llama3.1:8b"
    assert request["body"]["stream"] is True
    assert request["body"]["stream_options"] == {"include_usage": True}
    assert request["body"]["messages"][0] == {"role": "system", "content": "be terse"}
    (row,) = _rows(tmp_path)
    assert (row.provider, row.model) == ("ollama", "llama3.1:8b")
    assert (row.prompt_tokens, row.completion_tokens, row.cache_read_tokens) == (40, 9, 0)
    assert row.cost_usd == 0.0
    assert row.price_source == "self-hosted"
    assert row.error is None


def test_openai_compatible_sends_the_first_key_and_splits_cached_tokens(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", "openai_compatible")
    monkeypatch.setenv("REPOACH_OPENAI_API_KEY", "first-key-value")
    monkeypatch.setenv("OPENAI_API_KEY", "second-key-value")
    _models(monkeypatch, "served-model")
    with FakeChatServer(text="ok", required_key="first-key-value") as server:
        monkeypatch.setenv("REPOACH_OPENAI_BASE_URL", f"{server.root}/v1/")
        out = AgentLoop(capability=CapabilityTier.OPUS).run_oneshot("x")
    assert out.text == "ok"
    assert server.requests[0]["path"] == "/v1/chat/completions"
    assert server.requests[0]["headers"]["authorization"] == "Bearer first-key-value"
    (row,) = _rows(tmp_path)
    assert (row.provider, row.model) == ("openai_compatible", "served-model")
    assert (row.prompt_tokens, row.cache_read_tokens, row.completion_tokens) == (32, 8, 9)
    assert row.cost_usd is None


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key_is_an_auth_error_naming_the_variable(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "rejected-key-value")
    _models(monkeypatch, "gpt-6-luna")
    with FakeChatServer(status=status) as server:
        _point_at(monkeypatch, "openai", server.root)
        with pytest.raises(ProviderAuthError) as info:
            AgentLoop(capability=CapabilityTier.SONNET).run_oneshot("x")
    assert str(status) in str(info.value)
    assert "OPENAI_API_KEY was rejected" in str(info.value)
    assert "rejected-key-value" not in str(info.value)
    assert len(server.requests) == 1
    (row,) = _rows(tmp_path)
    assert row.error is not None and "ProviderAuthError" in row.error
    assert (row.prompt_tokens, row.completion_tokens, row.cost_usd) == (0, 0, None)


def test_keyless_server_refusing_names_the_key_variables(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", "openai_compatible")
    _models(monkeypatch, "m")
    with FakeChatServer(required_key="needed") as server:
        monkeypatch.setenv("REPOACH_OPENAI_BASE_URL", server.root)
        with pytest.raises(ProviderAuthError, match="REPOACH_OPENAI_API_KEY, then OPENAI_API_KEY"):
            AgentLoop(capability=CapabilityTier.SONNET).run_oneshot("x")


def test_server_error_is_a_transport_error(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", "ollama")
    _models(monkeypatch, "m")
    with FakeChatServer(status=500) as server:
        _point_at(monkeypatch, "ollama", server.root)
        with pytest.raises(GatewayTransportError):
            AgentLoop(capability=CapabilityTier.SONNET).run_oneshot("x")


@pytest.mark.parametrize("status", [400, 404, 422])
def test_client_error_is_not_retryable(
    clone: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """A 400/404/422 (wrong model, path or body) fails the same way again: never retried."""
    monkeypatch.setenv("REPOACH_PROVIDER", "ollama")
    _models(monkeypatch, "m")
    with FakeChatServer(status=status) as server:
        _point_at(monkeypatch, "ollama", server.root)
        with pytest.raises(ProviderRequestError) as info:
            AgentLoop(capability=CapabilityTier.SONNET).run_oneshot("x")
    assert isinstance(info.value, GatewayError)
    assert not isinstance(info.value, GatewayTransportError)
    assert info.value.retryable is False
    assert len(server.requests) == 1


def test_unknown_model_ends_each_reviewer_after_one_request(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``review pr`` on a 404: one request per reviewer, every reviewer ERROR, rc 3."""
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)
    monkeypatch.setenv("REPOACH_PROVIDER", "ollama")
    _models(monkeypatch, "no-such-model")
    forge = _CountingForge()
    with FakeChatServer(status=404) as server:
        _point_at(monkeypatch, "ollama", server.root)
        result, _ = _review(monkeypatch, forge)
    assert result.exit_code == 3, result.output
    payload = json.loads(result.stdout)
    assert {review["verdict"] for review in payload["reviews"]} == {"ERROR"}
    assert len(server.requests) == len(payload["reviews"]) == 4


def test_gemini_native_posts_to_the_generative_language_root(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key-value")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key-value")
    _models(monkeypatch, "gemini-2.5-flash")
    with FakeChatServer(text="ok", required_key="gemini-key-value") as server:
        _point_at(monkeypatch, "gemini", f"{server.root}/v1beta/openai")
        out = AgentLoop(capability=CapabilityTier.HAIKU).run_oneshot("x")
    assert out.model_used == "gemini/gemini-2.5-flash"
    assert server.requests[0]["path"] == "/v1beta/openai/chat/completions"
    (row,) = _rows(tmp_path)
    assert (row.provider, row.model) == ("gemini", "gemini-2.5-flash")
    assert (row.prompt_tokens, row.cache_read_tokens, row.completion_tokens) == (32, 8, 9)
    assert (row.cost_usd, row.price_source) == (None, None)


def test_gemini_default_root_is_the_compatible_surface(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", "gemini")
    route = resolve_route()
    assert route.kind == "gemini"
    assert route_base_url(route) == "https://generativelanguage.googleapis.com/v1beta/openai"


def test_gemini_through_openrouter_is_the_preset_with_a_gemini_model(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key-value")
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-be-sent")
    _models(monkeypatch, "google/gemini-3.8-flash")
    with FakeChatServer(text="ok", required_key="router-key-value") as server:
        _point_at(monkeypatch, "openrouter", f"{server.root}/api/v1")
        out = AgentLoop(capability=CapabilityTier.SONNET).run_oneshot("x")
    assert out.model_used == "openrouter/google/gemini-3.8-flash"
    assert server.requests[0]["path"] == "/api/v1/chat/completions"
    assert server.requests[0]["body"]["model"] == "google/gemini-3.8-flash"
    (row,) = _rows(tmp_path)
    assert (row.provider, row.model, row.cost_usd) == (
        "openrouter",
        "google/gemini-3.8-flash",
        None,
    )


@pytest.mark.parametrize(
    ("provider", "env", "expected"),
    [
        ("openai_compatible", {}, "REPOACH_MODEL_OPUS / [provider].opus"),
        ("openai_compatible", {"MODELS": "m"}, "no base URL"),
        ("openrouter", {"MODELS": "m"}, "REPOACH_OPENROUTER_API_KEY, then OPENROUTER_API_KEY"),
        ("gemini", {"MODELS": "m"}, "REPOACH_GEMINI_API_KEY, then GEMINI_API_KEY, then GOOGLE"),
    ],
)
def test_missing_setting_fails_before_any_call(
    clone: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    env: dict[str, str],
    expected: str,
) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", provider)
    if "MODELS" in env:
        _models(monkeypatch, env["MODELS"])
    with pytest.raises(ProviderConfigError, match=expected.replace("[", r"\[").replace("]", r"\]")):
        AgentLoop(capability=CapabilityTier.SONNET)


@pytest.mark.parametrize("provider", ["ollama", "openrouter", "gemini"])
def test_a_base_url_beside_a_preset_or_gemini_is_refused(
    clone: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", provider)
    monkeypatch.setenv("REPOACH_OPENAI_BASE_URL", "http://elsewhere.invalid:1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    _models(monkeypatch, "m")
    with pytest.raises(
        ProviderConfigError,
        match=f"preset {provider} fixes its host; use kind=openai_compatible to point elsewhere",
    ):
        AgentLoop(capability=CapabilityTier.SONNET)


def test_presets_only_set_a_base_url_and_key_names() -> None:
    assert {name: preset.base_url for name, preset in PRESETS.items()} == {
        "openai": "https://api.openai.com/v1",
        "ollama": "http://localhost:11434/v1",
        "openrouter": "https://openrouter.ai/api/v1",
        "groq": "https://api.groq.com/openai/v1",
        "deepseek": "https://api.deepseek.com/v1",
        "mistral": "https://api.mistral.ai/v1",
    }
    assert PRESETS["ollama"].credential_names == ()
    assert PRESETS["openai"].credential_names == ("REPOACH_OPENAI_API_KEY", "OPENAI_API_KEY")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://localhost:11434", "http://localhost:11434/v1"),
        ("http://localhost:11434/", "http://localhost:11434/v1"),
        ("http://localhost:11434/v1/", "http://localhost:11434/v1"),
        ("https://openrouter.ai/api", "https://openrouter.ai/api/v1"),
        ("https://api.groq.com/openai/v1", "https://api.groq.com/openai/v1"),
        (
            "https://generativelanguage.googleapis.com/v1beta/openai/",
            "https://generativelanguage.googleapis.com/v1beta/openai",
        ),
    ],
)
def test_base_url_without_a_version_gets_v1(raw: str, expected: str) -> None:
    assert openai_compatible_base_url(raw) == expected


def test_flag_outranks_env_outranks_file(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (clone / "repoach.toml").write_text('[repoach]\nschema = 1\n\n[provider]\nkind = "anthropic"\n')
    assert (resolve_route().configured, resolve_route().source) == ("anthropic", "file")
    monkeypatch.setenv("REPOACH_PROVIDER", "gemini")
    assert (resolve_route().configured, resolve_route().source) == ("gemini", "env")
    set_provider_override("ollama")
    route = resolve_route()
    assert (route.configured, route.source, route.kind, route.preset) == (
        "ollama",
        "flag",
        "openai_compatible",
        "ollama",
    )
    with pytest.raises(ProviderConfigError, match="unknown provider 'nope'"):
        set_provider_override("nope")


def test_review_pr_with_the_provider_flag_end_to_end(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``review pr --provider ollama`` over a fake server: four reviews, usage in JSON and on stderr."""
    monkeypatch.setenv("REPOACH_PROVIDER", "anthropic")
    _models(monkeypatch, "llama3.1:8b")
    forge = _CountingForge()

    def select(**_kwargs: Any) -> _CountingForge:
        return forge

    monkeypatch.setattr("repoach.cli.review_cmds.select_forge", select)
    monkeypatch.setattr("repoach.review.orchestrator.select_forge", select)
    with FakeChatServer(text=APPROVE_JSON, shape="ollama") as server:
        _point_at(monkeypatch, "ollama", server.root)
        result = CliRunner().invoke(
            review_app,
            ["pr", "42", "--dry-run", "--forge", "github", "--provider", "ollama"],
        )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["final_verdict"] == "APPROVE"
    assert len(payload["reviews"]) == 4
    usage = payload["usage"]
    reviewers = {entry["reviewer"] for entry in usage["per_reviewer"]}
    assert {"Architect", "Sentinel", "Tester", "Scribe"} <= reviewers
    assert usage["providers"] == ["ollama"]
    assert usage["total"]["calls"] == len(server.requests) >= 4
    assert usage["total"]["prompt_tokens"] == 40 * usage["total"]["calls"]
    assert usage["total"]["cost_usd"] == 0.0
    summary = [line for line in result.stderr.splitlines() if line.startswith("repoach: usage:")]
    assert len(summary) == 1
    assert f"{usage['total']['calls']} calls" in summary[0]
    assert "$0.0000 (prices read 2026-10-21)" in summary[0]
    rows = _rows(tmp_path)
    assert {row.run_id for row in rows} == {usage["run_id"]}
    assert {row.pr_number for row in rows} == {42}


def test_review_pr_with_an_unknown_provider_flag_exits_2(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge = _CountingForge()
    monkeypatch.setattr("repoach.cli.review_cmds.select_forge", lambda **_kw: forge)
    result = CliRunner().invoke(review_app, ["pr", "42", "--dry-run", "--provider", "nope"])
    assert result.exit_code == 2
    assert "unknown provider 'nope'" in result.stderr
    assert forge.transport_calls == []


def test_nothing_configured_is_one_line_rc_2_before_any_forge_call(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forge = _CountingForge()
    result, _ = _review(monkeypatch, forge)
    assert result.exit_code == 2
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert lines == [f"repoach: {NO_PROVIDER_MESSAGE}"]
    assert forge.transport_calls == []


def test_no_provider_message_names_every_kind_preset_and_the_flag() -> None:
    assert "--provider" in NO_PROVIDER_MESSAGE
    assert "REPOACH_PROVIDER" in NO_PROVIDER_MESSAGE
    for name in SELECTABLE:
        assert name in NO_PROVIDER_MESSAGE


@pytest.mark.parametrize("command", [["review", "pr"], ["review", "gate"], ["ci", "review"]])
def test_provider_flag_help_shows_the_toml_key(command: list[str]) -> None:
    """Rich markup would eat an unescaped ``[provider]``; the help must print it."""
    result = CliRunner().invoke(main_app, [*command, "--help"], env={"COLUMNS": "250"})
    assert result.exit_code == 0
    assert "REPOACH_PROVIDER and [provider].kind" in result.stdout
