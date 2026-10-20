"""The ``anthropic`` provider: the Messages API in-process, no gateway (SP-PROVIDER-DIRECT-ANTHROPIC AC5).

The model boundary is a real HTTP server on the loopback
(:class:`tests.provider_fakes.FakeAnthropicServer`) reached through
``REPOACH_ANTHROPIC_BASE_URL``; the gateway's own
:class:`~repoach.llm_proxy.providers.anthropic_messages.AnthropicMessagesTransport`
speaks to it exactly as it speaks to the API.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from repoach.agent_engine.adapters import GatewayError, GatewayTransportError
from repoach.agent_engine.agent_loop import AgentLoop
from repoach.core import config as core_config
from repoach.llm.capability import CapabilityTier
from repoach.review.reviewer import Architect, ReviewVerdict
from tests.provider_fakes import FakeAnthropicServer
from tests.unit.test_provider_select import PROVIDER_ENV
from tests.unit.test_universal_install import _repo


@pytest.fixture
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("REPOACH_LLM_PROXY_BASE_URL", raising=False)
    monkeypatch.setenv("REPOACH_PROVIDER", "anthropic")
    repo = _repo(tmp_path / "clone")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(core_config, "_settings", None)
    return repo


def test_oneshot_calls_v1_messages_with_the_first_key(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPOACH_ANTHROPIC_API_KEY", "first-key-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "second-key-value")
    with FakeAnthropicServer(text="pong") as server:
        monkeypatch.setenv("REPOACH_ANTHROPIC_BASE_URL", server.base_url)
        out = AgentLoop(capability=CapabilityTier.SONNET, max_tokens=99).run_oneshot(
            "ping", system="be terse"
        )
    assert out.text == "pong"
    assert out.model_used == "anthropic/claude-sonnet-5"
    assert out.tokens_used == 12 + 1 + 9
    (request,) = server.requests
    assert request["path"] == "/v1/messages"
    assert request["headers"]["x-api-key"] == "first-key-value"
    assert request["headers"]["anthropic-version"] == "2023-06-01"
    assert request["body"]["model"] == "claude-sonnet-5"
    assert request["body"]["system"] == "be terse"
    assert request["body"]["max_tokens"] == 99


def test_model_override_from_the_environment(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "only-key-value")
    monkeypatch.setenv("REPOACH_MODEL_OPUS", "claude-opus-override")
    with FakeAnthropicServer(text="ok") as server:
        monkeypatch.setenv("REPOACH_ANTHROPIC_BASE_URL", server.base_url)
        AgentLoop(capability=CapabilityTier.OPUS).run_oneshot("x")
    assert server.requests[0]["body"]["model"] == "claude-opus-override"
    assert server.requests[0]["headers"]["x-api-key"] == "only-key-value"


def test_rejected_key_names_the_variable_never_the_value(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "rejected-key-value")
    with FakeAnthropicServer(status=401) as server:
        monkeypatch.setenv("REPOACH_ANTHROPIC_BASE_URL", server.base_url)
        with pytest.raises(GatewayError) as info:
            AgentLoop(capability=CapabilityTier.SONNET).run_oneshot("x")
    assert not isinstance(info.value, GatewayTransportError)
    assert "ANTHROPIC_API_KEY" in str(info.value)
    assert "rejected-key-value" not in str(info.value)
    assert len(server.requests) == 1


def test_server_error_is_a_transport_error(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACH_ANTHROPIC_API_KEY", "some-key-value")
    with FakeAnthropicServer(status=500) as server:
        monkeypatch.setenv("REPOACH_ANTHROPIC_BASE_URL", server.base_url)
        with pytest.raises(GatewayTransportError, match="500"):
            AgentLoop(capability=CapabilityTier.SONNET).run_oneshot("x")


def test_unreachable_api_is_a_transport_error(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv("REPOACH_ANTHROPIC_API_KEY", "some-key-value")
    monkeypatch.setenv("REPOACH_ANTHROPIC_BASE_URL", f"http://127.0.0.1:{port}/v1")
    with pytest.raises(GatewayTransportError):
        AgentLoop(capability=CapabilityTier.SONNET).run_oneshot("x")


def test_rejected_key_ends_the_reviewer_after_one_request_with_the_hint(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 401 is not retried by the reviewer's backoff; the hint reaches the summary and the log."""
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "rejected-key-value")
    with FakeAnthropicServer(status=401) as server:
        monkeypatch.setenv("REPOACH_ANTHROPIC_BASE_URL", server.base_url)
        reviewer = Architect()
        with capture_logs() as logs:
            outcome = reviewer.review_diff("diff --git a/x.py b/x.py\n+x = 1\n", pr_number=1)
    assert len(server.requests) == 1
    assert outcome.verdict is ReviewVerdict.ERROR
    assert "ANTHROPIC_API_KEY was rejected; replace it" in outcome.summary
    assert "rejected-key-value" not in outcome.summary
    fatal = [entry for entry in logs if entry["event"] == "review.bot.attempt_fatal"]
    assert len(fatal) == 1
    assert "replace it" in fatal[0]["message"]
    assert fatal[0]["log_level"] == "error"
