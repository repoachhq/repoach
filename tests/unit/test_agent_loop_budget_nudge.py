"""The loop announces its dying budget and refuses a leaked wrap-up.

Observed live (SP-DEV-STEP-PREFLIGHT, 2026-07-04): three Developer
dispatches (~2.6M tokens combined) each spent every turn exploring and
finalized with zero writes — their own wrap-up summaries stated the
implementation they never issued — and one wrap-up recorded deepseek's
raw DSML markup as the official dispatch summary. These tests pin the
two counter-measures: an in-band budget warning while turns remain to
act on it, and a one-shot wrap-up retry that skips the leaking model.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from ferova.agent_engine.agent_loop import AgentLoop, ToolDef
from ferova.llm_proxy.api.models.agent_v1 import (
    AgentResponse,
    TextBlock,
    ToolCallBlock,
    Usage,
)


@pytest.fixture(autouse=True)
def _stub_proxy_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep AgentLoop constructible without the operator ``.env``."""
    monkeypatch.setattr(
        "ferova.agent_engine.agent_loop.get_settings",
        lambda: SimpleNamespace(
            llm_proxy_base_url="http://localhost:8082",
            llm_proxy_auth_token=SecretStr("test-token"),
        ),
    )


def _text_response(text: str) -> AgentResponse:
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


def _tool_call_response(call_id: str) -> AgentResponse:
    return AgentResponse(
        schema_version="1",
        capability="sonnet",
        stop_reason="tool_use",
        model_used="fake/sonnet",
        content=[ToolCallBlock(id=call_id, name="noop", args={})],
        usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        elapsed_ms=10,
        trace=[],
    )


class _ScriptedClient:
    """Gateway client returning queued responses, recording each call."""

    def __init__(self, responses: list[AgentResponse]) -> None:
        self._queue = list(responses)
        self.calls: list[dict] = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        return self._queue.pop(0)


def _tool() -> ToolDef:
    return ToolDef(
        name="noop",
        description="does nothing",
        parameters_schema={"type": "object", "properties": {}},
        callable_fn=lambda **kwargs: "ok",
    )


def _loop_with(client, max_turns: int) -> AgentLoop:
    loop = AgentLoop(model_chain=("claude-sonnet-4-6",), max_turns=max_turns)
    loop._client = client
    return loop


def _user_text(message) -> str:
    return "".join(b.text for b in message.content if isinstance(b, TextBlock))


class TestBudgetNudge:
    def test_nudge_lands_alongside_the_tool_results(self) -> None:
        """With 3 turns left the results message also carries the warning."""
        client = _ScriptedClient(
            [
                _tool_call_response("t1"),
                _tool_call_response("t2"),
                _text_response("done"),
            ]
        )
        output = _loop_with(client, max_turns=5).run("go", tools=[_tool()])
        assert output.text == "done"
        assert "Budget notice: only 3 of 5 turns remain" in _user_text(
            client.calls[2]["messages"][-1]
        )

    def test_no_nudge_while_the_budget_is_healthy(self) -> None:
        """Turns far from the thresholds carry tool results only."""
        client = _ScriptedClient(
            [
                _tool_call_response("t1"),
                _tool_call_response("t2"),
                _text_response("done"),
            ]
        )
        _loop_with(client, max_turns=30).run("go", tools=[_tool()])
        for call in client.calls[1:]:
            assert "Budget notice" not in _user_text(call["messages"][-1])


class TestWrapUpLeakScreen:
    _DSML_LEAK = "<｜｜DSML｜｜tool_calls>read_file(path=...)"

    def test_leaked_wrap_up_is_retried_without_the_leaker(self) -> None:
        """The recorded summary is the retry's prose, never the markup."""
        client = _ScriptedClient(
            [
                _tool_call_response("t1"),
                _tool_call_response("t2"),
                _text_response(self._DSML_LEAK),
                _text_response("summary: explored but wrote nothing"),
            ]
        )
        output = _loop_with(client, max_turns=2).run("go", tools=[_tool()])
        assert output.text == "summary: explored but wrote nothing"
        assert len(client.calls) == 4
        assert client.calls[3]["skip_models"] == frozenset({"fake/sonnet"})
        assert "prose only" in _user_text(client.calls[3]["messages"][-1])

    def test_clean_wrap_up_passes_through_with_session_skips(self) -> None:
        """A prose wrap-up is final; the call carries the session skip set."""
        client = _ScriptedClient(
            [
                _tool_call_response("t1"),
                _tool_call_response("t2"),
                _text_response("honest summary"),
            ]
        )
        output = _loop_with(client, max_turns=2).run("go", tools=[_tool()])
        assert output.text == "honest summary"
        assert len(client.calls) == 3
        assert client.calls[2]["skip_models"] == frozenset()
