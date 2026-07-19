"""SP-PLANNER-AGENT — per-turn transport retry in the tool loop.

NIM intermittently 502s tool-carrying requests; without a per-turn
retry a multi-turn exploration dies on its first unlucky turn. These
tests drive :meth:`AgentLoop.run` with a fake gateway client that
fails N times before succeeding, and pin that semantic outputs are
never retried.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from repoach.agent_engine.adapters import GatewayTransportError
from repoach.agent_engine.agent_loop import AgentLoop, ToolDef
from repoach.llm_proxy.api.models.agent_v1 import (
    AgentResponse,
    TextBlock,
    Usage,
)

_NO_WAIT = (0.0, 0.0, 0.0)


@pytest.fixture(autouse=True)
def _stub_proxy_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep AgentLoop constructible without the operator ``.env``.

    SP-PROXY-SECURE-DEFAULTS removed the implicit ``freecc`` token
    fallback, so a tokenless environment (CI) now refuses to build the
    loop — these tests target retry behaviour, not auth.
    """
    monkeypatch.setattr(
        "repoach.agent_engine.agent_loop.get_settings",
        lambda: SimpleNamespace(
            llm_proxy_base_url="http://localhost:8082",
            llm_proxy_auth_token=SecretStr("test-token"),
        ),
    )


def _final_response(text: str = "done") -> AgentResponse:
    return AgentResponse(
        schema_version="1",
        capability="sonnet",
        stop_reason="end_turn",
        model_used="fake/sonnet",
        content=[TextBlock(text=text)],
        usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        elapsed_ms=10,
        trace=[],
    )


class _FlakyClient:
    """Gateway client failing with transport errors N times, then OK."""

    def __init__(self, failures_before_success: int) -> None:
        self.failures_left = failures_before_success
        self.calls = 0

    def call(self, **kwargs):
        self.calls += 1
        if self.failures_left > 0:
            self.failures_left -= 1
            raise GatewayTransportError("proxy returned 502: chain exhausted")
        return _final_response()


def _tool() -> ToolDef:
    return ToolDef(
        name="noop",
        description="never called",
        parameters_schema={"type": "object", "properties": {}},
        callable_fn=lambda **kwargs: "ok",
    )


def _loop_with(client) -> AgentLoop:
    loop = AgentLoop(model_chain=("claude-sonnet-4-6",))
    loop._client = client
    return loop


class TestTurnRetry:
    def test_one_transport_failure_is_absorbed(self) -> None:
        client = _FlakyClient(failures_before_success=1)
        with patch.object(AgentLoop, "_TURN_RETRY_BACKOFFS_S", _NO_WAIT):
            output = _loop_with(client).run("go", tools=[_tool()])
        assert output.text == "done"
        assert client.calls == 2

    def test_two_transport_failures_are_absorbed(self) -> None:
        client = _FlakyClient(failures_before_success=2)
        with patch.object(AgentLoop, "_TURN_RETRY_BACKOFFS_S", _NO_WAIT):
            output = _loop_with(client).run("go", tools=[_tool()])
        assert output.text == "done"
        assert client.calls == 3

    def test_exhausted_retries_reraise_the_transport_error(self) -> None:
        client = _FlakyClient(failures_before_success=99)
        with patch.object(AgentLoop, "_TURN_RETRY_BACKOFFS_S", _NO_WAIT):
            try:
                _loop_with(client).run("go", tools=[_tool()])
            except GatewayTransportError:
                assert client.calls == 3
            else:
                raise AssertionError("expected GatewayTransportError")

    def test_clean_first_call_makes_exactly_one_request(self) -> None:
        client = _FlakyClient(failures_before_success=0)
        with patch.object(AgentLoop, "_TURN_RETRY_BACKOFFS_S", _NO_WAIT):
            output = _loop_with(client).run("go", tools=[_tool()])
        assert output.text == "done"
        assert client.calls == 1
