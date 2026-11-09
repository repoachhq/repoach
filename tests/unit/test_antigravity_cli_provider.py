"""Tests for SP-PROVIDER-ANTIGRAVITY-CLI — Antigravity CLI (agy) subprocess transport."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from repoach.llm_proxy.config.provider_ids import SUPPORTED_PROVIDER_IDS
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.antigravity import AntigravityProvider
from repoach.llm_proxy.providers.antigravity.client import (
    CLI_ERROR_TEXT_MAX_CHARS,
    agy_exit_error,
    agy_result_error,
)
from repoach.llm_proxy.providers.base import ProviderConfig
from repoach.llm_proxy.providers.catalog import classify_thinking
from repoach.llm_proxy.providers.exceptions import OutputCapExceededError, ProviderError
from repoach.llm_proxy.providers.registry import create_provider


def _write_fake_agy_script(
    tmp_path: Path,
    *,
    response_text: str = "PONG",
    status: str = "SUCCESS",
    error_text: str = "",
    exit_code: int = 0,
    input_tokens: int = 7385,
    cache_read_tokens: int = 7000,
    output_tokens: int = 12,
    record_file: Path | None = None,
) -> Path:
    """Write an executable fake ``agy`` script that speaks ``stream-json``."""
    cli = tmp_path / "agy"
    record_str = str(record_file) if record_file is not None else ""
    body = "\n".join(
        [
            f"#!{sys.executable}",
            "import json, pathlib, sys",
            f"record_path = {record_str!r}",
            "stdin_raw = sys.stdin.read()",
            "if record_path:",
            "    pathlib.Path(record_path).write_text(",
            "        json.dumps({'argv': sys.argv[1:], 'stdin': stdin_raw}),",
            "        encoding='utf-8',",
            "    )",
            "print(json.dumps({'event': 'init', 'init': {'conversation_id': 'c1'}}))",
            "print(json.dumps({'event': 'step_update', 'step_update': {'step_index': 0}}))",
            "result_obj = {",
            "    'conversation_id': 'c1',",
            f"    'status': {status!r},",
            f"    'response': {response_text!r},",
            f"    'error': {error_text!r},",
            "    'duration_seconds': 0.2,",
            "    'num_turns': 1,",
            "    'usage': {",
            f"        'input_tokens': {input_tokens},",
            f"        'output_tokens': {output_tokens},",
            "        'thinking_tokens': 0,",
            f"        'cache_read_tokens': {cache_read_tokens},",
            f"        'total_tokens': {input_tokens + output_tokens},",
            "    },",
            "}",
            "print(json.dumps({'event': 'result', 'result': result_obj}))",
            f"sys.exit({exit_code})",
        ]
    )
    cli.write_text(body + "\n", encoding="utf-8")
    cli.chmod(0o755)
    return cli


def _parse_sse_events(chunks: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """Parse SSE event strings into ``(event_name, data_dict)`` pairs."""
    events: list[tuple[str, dict[str, Any]]] = []
    raw = "".join(chunks)
    for block in raw.strip().split("\n\n"):
        event_name = ""
        data_obj: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line.removeprefix("event: ").strip()
            elif line.startswith("data: "):
                data_obj = json.loads(line.removeprefix("data: ").strip())
        if event_name:
            events.append((event_name, data_obj))
    return events


def test_antigravity_descriptor_and_registry_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1: antigravity is in SUPPORTED_PROVIDER_IDS and builds an AntigravityProvider."""
    monkeypatch.setenv("REPOACH_ANTIGRAVITY_CLI_PATH", "/custom/agy")
    monkeypatch.setenv("REPOACH_ANTIGRAVITY_DEFAULT_MODEL", "gemini-4-argon-medium")
    monkeypatch.setenv("REPOACH_ANTIGRAVITY_SUBPROCESS_TIMEOUT", "300.0")
    assert "antigravity" in SUPPORTED_PROVIDER_IDS
    assert classify_thinking("antigravity/gemini-4-argon-high") == "hybrid"
    settings = Settings(_env_file=None)
    provider = create_provider("antigravity", settings)
    assert isinstance(provider, AntigravityProvider)
    assert provider._default_model == "gemini-4-argon-medium"
    assert provider._subprocess_timeout == 300.0


@pytest.mark.asyncio
async def test_antigravity_stream_response_parses_stream_json_result_and_usage(
    tmp_path: Path,
) -> None:
    """AC2: stream_response parses stream-json result and copies token usage accurately."""
    record_file = tmp_path / "recorded.json"
    cli = _write_fake_agy_script(
        tmp_path,
        response_text="Hello from Antigravity",
        input_tokens=7385,
        cache_read_tokens=7000,
        output_tokens=15,
        record_file=record_file,
    )
    provider = AntigravityProvider(
        ProviderConfig(api_key="antigravity-cli"),
        cli_path=str(cli),
    )
    request = SimpleNamespace(
        model="antigravity/sonnet",
        system="Be concise.",
        messages=[{"role": "user", "content": "Say hello"}],
        tools=None,
        thinking={"type": "disabled"},
    )
    chunks = [
        chunk async for chunk in provider.stream_response(request, input_tokens=10, request_id="r1")
    ]
    events = _parse_sse_events(chunks)
    event_names = [name for name, _ in events]
    assert event_names == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    delta_payload = dict(events)["content_block_delta"]
    assert delta_payload["delta"]["text"] == "Hello from Antigravity"
    msg_delta = dict(events)["message_delta"]
    assert msg_delta["delta"]["stop_reason"] == "end_turn"
    assert msg_delta["usage"]["input_tokens"] == 385
    assert msg_delta["usage"]["cache_read_input_tokens"] == 7000
    assert msg_delta["usage"]["output_tokens"] == 15

    recorded = json.loads(record_file.read_text(encoding="utf-8"))
    assert recorded["argv"] == [
        "--disable-slash-commands",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--model",
        "gemini-4-argon-medium",
        "--effort",
        "low",
    ]
    stdin_event = json.loads(recorded["stdin"].strip())
    assert stdin_event["event"] == "user"
    assert "[system]\nBe concise." in stdin_event["message"]["content"]
    assert "[user]\nSay hello" in stdin_event["message"]["content"]


@pytest.mark.asyncio
async def test_antigravity_emulates_tool_use_blocks(tmp_path: Path) -> None:
    """AC3: <tool_use> blocks in result.response become native tool_use SSE blocks."""
    cli = _write_fake_agy_script(
        tmp_path,
        response_text='Let me check.\n<tool_use>{"name": "read_file", "args": {"path": "a.py"}}</tool_use>',
        output_tokens=24,
    )
    provider = AntigravityProvider(
        ProviderConfig(api_key="antigravity-cli"),
        cli_path=str(cli),
    )
    request = SimpleNamespace(
        model="antigravity/opus",
        system="",
        messages=[{"role": "user", "content": "Inspect a.py"}],
        tools=[
            {
                "name": "read_file",
                "description": "Read a file",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        ],
        thinking=None,
    )
    chunks = [chunk async for chunk in provider.stream_response(request, input_tokens=20)]
    events = _parse_sse_events(chunks)
    tool_starts = [
        data
        for name, data in events
        if name == "content_block_start" and data.get("content_block", {}).get("type") == "tool_use"
    ]
    assert len(tool_starts) == 1
    assert tool_starts[0]["content_block"]["name"] == "read_file"
    msg_delta = [data for name, data in events if name == "message_delta"][-1]
    assert msg_delta["delta"]["stop_reason"] == "tool_use"


@pytest.mark.asyncio
async def test_antigravity_error_status_and_nonzero_exit_surface_error_text(
    tmp_path: Path,
) -> None:
    """AC4: Non-zero exit or status != SUCCESS raises ProviderError with truncated error text."""
    cli = _write_fake_agy_script(
        tmp_path,
        response_text="",
        status="ERROR",
        error_text="invalid model selection: unknown model foo",
        exit_code=1,
    )
    provider = AntigravityProvider(
        ProviderConfig(api_key="antigravity-cli"),
        cli_path=str(cli),
    )
    request = SimpleNamespace(
        model="antigravity/foo",
        system="",
        messages=[{"role": "user", "content": "Hi"}],
        tools=None,
        thinking=None,
    )
    with pytest.raises(ProviderError) as exc_info:
        async for _ in provider.stream_response(request, request_id="req_42"):
            pass
    assert "agy CLI exited 1: invalid model selection: unknown model foo" in str(exc_info.value)
    assert "(request_id=req_42)" in str(exc_info.value)

    cap_err = agy_result_error(
        {"status": "ERROR", "api_error": "max_output_tokens", "error": "x" * 2500},
        "req_7",
        prefix="agy CLI exited 1: ",
    )
    assert isinstance(cap_err, OutputCapExceededError)
    assert (
        str(cap_err)
        == "agy CLI exited 1: " + "x" * CLI_ERROR_TEXT_MAX_CHARS + " (request_id=req_7)"
    )

    fallback_err = agy_exit_error(2, "non-json stdout", "fatal stderr", "req_9")
    assert str(fallback_err) == "agy CLI exited 2: fatal stderr (request_id=req_9)"


def test_antigravity_model_mapping_aliases_and_explicit_slugs() -> None:
    """AC5: Tier aliases map to default slugs and explicit agy slugs pass through unchanged."""
    provider = AntigravityProvider(ProviderConfig(api_key="antigravity-cli"))
    assert provider._cli_model_for("opus") == "gemini-4-argon-high"
    assert provider._cli_model_for("antigravity/opus") == "gemini-4-argon-high"
    assert provider._cli_model_for("sonnet") == "gemini-4-argon-medium"
    assert provider._cli_model_for("antigravity/sonnet") == "gemini-4-argon-medium"
    assert provider._cli_model_for("haiku") == "gemini-3.8-flash-medium"
    assert provider._cli_model_for("antigravity/haiku") == "gemini-3.8-flash-medium"
    assert provider._cli_model_for("gemini-3.8-flash-low") == "gemini-3.8-flash-low"
    assert provider._cli_model_for("antigravity/claude-sonnet-5-5-high") == "claude-sonnet-5-5-high"
    assert provider._cli_model_for("gpt-oss-120b-medium") == "gpt-oss-120b-medium"
    assert provider._cli_model_for("claude-sonnet-4-5") == "gemini-4-argon-medium"
    assert provider._cli_model_for("") == "gemini-3.8-flash-medium"
