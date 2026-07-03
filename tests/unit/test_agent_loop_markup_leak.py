"""Leaked tool-call markup is a reprompt, never a final answer.

Observed live (SP-ORCH-DOCSTRING, 2026-07-03): minimax-m3 emitted its
``edit_file`` calls as plain-text ``]<]minimax[>[<tool_call>`` markup,
the loop took the response for a final answer, and the Developer
session ended "without writing any file" — four dispatches, ~1.1M
tokens, zero edits. deepseek's DSML variant surfaced earlier in
persisted coder summaries. These tests pin the corrective behaviour:
the loop re-prompts the model to re-issue the actions through the
native tool interface instead of finalising on the leak.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from ferova.agent_engine.agent_loop import AgentLoop, ToolDef
from ferova.llm_proxy.api.models.agent_v1 import (
    AgentResponse,
    TextBlock,
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
        description="never called",
        parameters_schema={"type": "object", "properties": {}},
        callable_fn=lambda **kwargs: "ok",
    )


def _loop_with(client) -> AgentLoop:
    loop = AgentLoop(model_chain=("claude-sonnet-4-6",))
    loop._client = client
    return loop


_MINIMAX_LEAK = (
    'I will make the changes now.]<]minimax[>[<tool_call> ]<]minimax[>[<invoke name="edit_file">'
)
_DSML_LEAK = "<｜｜DSML｜｜tool_calls>edit_file(path=...)"


def test_minimax_markup_triggers_a_corrective_reprompt() -> None:
    """The leak is re-prompted; the next clean answer is the final one."""
    client = _ScriptedClient([_text_response(_MINIMAX_LEAK), _text_response("done")])
    output = _loop_with(client).run("go", tools=[_tool()])
    assert output.text == "done"
    assert len(client.calls) == 2
    reprompt = client.calls[1]["messages"][-1]
    joined = "".join(b.text for b in reprompt.content if isinstance(b, TextBlock))
    assert "native tool interface" in joined


def test_dsml_markup_is_also_caught() -> None:
    """deepseek's DSML variant is treated identically."""
    client = _ScriptedClient([_text_response(_DSML_LEAK), _text_response("done")])
    output = _loop_with(client).run("go", tools=[_tool()])
    assert output.text == "done"
    assert len(client.calls) == 2


def test_plain_final_answers_are_untouched() -> None:
    """A normal no-tool answer still finalises on the first turn."""
    client = _ScriptedClient([_text_response("all gates green, step complete")])
    output = _loop_with(client).run("go", tools=[_tool()])
    assert output.text == "all gates green, step complete"
    assert len(client.calls) == 1


def test_persistent_leaker_is_skipped_from_the_chain() -> None:
    """Two consecutive leaks escalate to skip_models for the next turns.

    Attempt 3 of SP-ORCH-DOCSTRING proved the reprompt alone cannot
    save a session when the model leaks on every turn (8 corrective
    reprompts, still zero files written): the provider was plainly not
    honouring the tool interface. After two consecutive leaks the loop
    now bypasses that model via SP-PROXY-SEMANTIC-FAILOVER's
    skip_models so the chain serves the next candidate.
    """
    leak1 = _text_response(_MINIMAX_LEAK)
    leak2 = _text_response(_MINIMAX_LEAK)
    clean = _text_response("done")
    client = _ScriptedClient([leak1, leak2, clean])
    output = _loop_with(client).run("go", tools=[_tool()])
    assert output.text == "done"
    assert len(client.calls) == 3
    assert client.calls[0]["skip_models"] == frozenset()
    assert client.calls[1]["skip_models"] == frozenset()
    assert client.calls[2]["skip_models"] == frozenset({"fake/sonnet"})
