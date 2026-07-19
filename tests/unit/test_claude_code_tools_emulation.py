"""SP-CC-EMUL-HARDEN — end-to-end claude_code tool emulation path.

Drives :meth:`ClaudeCodeProvider.stream_response` with a stubbed
``claude`` subprocess whose result text follows the tools-appendix
protocol, and asserts the SSE stream the AgentLoop consumes carries a
NATIVE ``tool_use`` content block with ``stop_reason: "tool_use"`` —
the contract the rest of the proxy (and the future Planner fallback)
relies on. Also pins the appendix rendering trigger in
:meth:`_build_prompt`.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from repoach.llm_proxy.providers.base import ProviderConfig
from repoach.llm_proxy.providers.claude_code.client import ClaudeCodeProvider

_READ_FILE_TOOL = {
    "name": "read_file",
    "description": "Read a repo file.",
    "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
}


def _provider() -> ClaudeCodeProvider:
    return ClaudeCodeProvider(ProviderConfig(api_key="unused"), cli_path="claude-stub")


def _request(tools: list[dict] | None) -> Any:
    return SimpleNamespace(
        model="claude-sonnet-4-6",
        system="You are the Planner.",
        messages=[{"role": "user", "content": "Explore the repo."}],
        tools=tools,
    )


class _FakeProcess:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.returncode = 0
        self.stdin_received: bytes | None = None

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        self.stdin_received = input
        return json.dumps(self._payload).encode(), b""


def _collect_sse(
    payload: dict,
    tools: list[dict] | None,
    spawn_log: list[tuple[tuple[Any, ...], _FakeProcess]] | None = None,
) -> list[dict]:
    provider = _provider()

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        proc = _FakeProcess(payload)
        if spawn_log is not None:
            spawn_log.append((args, proc))
        return proc

    async def run() -> list[str]:
        events: list[str] = []
        with patch(
            "repoach.llm_proxy.providers.claude_code.client.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ):
            async for event in provider.stream_response(_request(tools)):
                events.append(event)
        return events

    raw_events = asyncio.run(run())
    parsed: list[dict] = []
    for event in raw_events:
        for line in event.splitlines():
            if line.startswith("data:"):
                parsed.append(json.loads(line[len("data:") :].strip()))
    return parsed


def test_protocol_compliant_text_yields_native_tool_use_block() -> None:
    payload = {
        "result": (
            "I need the file contents first.\n"
            '<tool_use>{"name": "read_file", "args": {"path": "src/a.py"}}</tool_use>'
        ),
        "usage": {"output_tokens": 42},
    }
    events = _collect_sse(payload, [_READ_FILE_TOOL])

    starts = [e for e in events if e.get("type") == "content_block_start"]
    tool_starts = [e for e in starts if e["content_block"]["type"] == "tool_use"]
    assert len(tool_starts) == 1
    assert tool_starts[0]["content_block"]["name"] == "read_file"

    deltas = [
        e
        for e in events
        if e.get("type") == "content_block_delta" and e["delta"].get("type") == "input_json_delta"
    ]
    assert json.loads(deltas[0]["delta"]["partial_json"]) == {"path": "src/a.py"}

    message_deltas = [e for e in events if e.get("type") == "message_delta"]
    assert message_deltas[-1]["delta"]["stop_reason"] == "tool_use"


def test_plain_answer_yields_end_turn_and_text_block() -> None:
    payload = {"result": "Final answer, no tools needed.", "usage": {"output_tokens": 7}}
    events = _collect_sse(payload, [_READ_FILE_TOOL])

    text_deltas = [
        e
        for e in events
        if e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta"
    ]
    assert any("Final answer" in e["delta"]["text"] for e in text_deltas)

    message_deltas = [e for e in events if e.get("type") == "message_delta"]
    assert message_deltas[-1]["delta"]["stop_reason"] == "end_turn"


def test_build_prompt_appends_tools_appendix_only_when_tools_present() -> None:
    provider = _provider()

    _, system_with = provider._build_prompt(_request([_READ_FILE_TOOL]))
    assert "TOOL CALL PROTOCOL" in system_with
    assert "read_file" in system_with
    assert system_with.startswith("You are the Planner.")

    _, system_without = provider._build_prompt(_request(None))
    assert "TOOL CALL PROTOCOL" not in system_without


def test_appendix_documents_the_exact_format_the_parser_accepts() -> None:
    appendix = ClaudeCodeProvider._render_tools_appendix([_READ_FILE_TOOL])
    assert '<tool_use>{"name": "<tool_name>", "args": {<json args>}}</tool_use>' in appendix
    assert "read_file" in appendix
    assert "required=path" in appendix


def test_prompt_travels_via_stdin_never_argv() -> None:
    """The conversation prompt reaches the CLI on stdin, not in argv.

    A Developer-session conversation serialized into one prompt
    exceeded ARG_MAX and the spawn died with OSError 'Argument list
    too long' — a naked 500 on the backstop hop
    (SP-DEV-PROMISE-DELIVERY step 2, 2026-07-05). argv keeps only the
    bounded flags and system prompt.
    """
    spawn_log: list[tuple[tuple[Any, ...], _FakeProcess]] = []
    _collect_sse({"result": "plain answer"}, None, spawn_log)

    assert len(spawn_log) == 1
    argv, proc = spawn_log[0]
    assert proc.stdin_received is not None
    assert b"Explore the repo." in proc.stdin_received
    assert not any("Explore the repo." in str(arg) for arg in argv)
