"""SP-CC-SYSPROMPT-FILE — system prompt travels via file, never argv.

Tests that :class:`ClaudeCodeProvider.stream_response` writes the
system prompt to a per-request file under ``self._workdir``, passes
``--system-prompt-file <path>`` in argv, scrubs the prompt text from
the ``CLAUDE_CODE_STREAM`` log line, and removes the file on every
exit path. Also pins the G3 init-time warning when ``shutil.which``
cannot resolve ``cli_path``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from loguru import logger as loguru_logger

from repoach.llm_proxy.providers.base import ProviderConfig
from repoach.llm_proxy.providers.claude_code.client import ClaudeCodeProvider


def _provider() -> ClaudeCodeProvider:
    """Return a provider with a stubbed CLI path."""
    return ClaudeCodeProvider(ProviderConfig(api_key="unused"), cli_path="claude-stub")


def _request(system_text: str) -> Any:
    """Build a minimal request with the given system prompt text."""
    return SimpleNamespace(
        model="claude-sonnet-4-6",
        system=system_text,
        messages=[{"role": "user", "content": "Hello."}],
        tools=None,
    )


class _FakeProcess:
    """Stub subprocess that returns a valid CLI JSON payload."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.returncode = 0
        self.stdin_received: bytes | None = None

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        self.stdin_received = input
        return json.dumps(self._payload).encode(), b""


def _collect_sse_and_capture(
    system_text: str,
    spawn_log: list[tuple[tuple[Any, ...], _FakeProcess]],
    log_records: list[Any],
    *,
    sysprompt_snapshots: list[bytes] | None = None,
) -> list[dict]:
    """Drive ``stream_response`` with fake exec, capturing spawn argv and loguru.

    Args:
        system_text: The system prompt string for the request.
        spawn_log: Mutable list that receives ``(argv_tuple, FakeProcess)``
            for each spawn.
        log_records: Mutable list that receives loguru ``Record`` dicts.
        sysprompt_snapshots: If provided, the file bytes at the
            ``--system-prompt-file`` path are read at spawn time and
            appended here (before the finally block removes the file).

    Returns:
        Parsed SSE events (list of dicts from ``data:`` lines).
    """
    provider = _provider()
    payload = {"result": "plain answer", "usage": {"output_tokens": 7}}

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        proc = _FakeProcess(payload)
        spawn_log.append((args, proc))
        if sysprompt_snapshots is not None:
            sp_path = _sysprompt_path_from_argv(args)
            if sp_path is not None:
                sysprompt_snapshots.append(sp_path.read_bytes())
        return proc

    sink_id = loguru_logger.add(
        lambda msg: log_records.append(msg.record),
        format="{message}",
        level="DEBUG",
    )
    try:

        async def run() -> list[str]:
            events: list[str] = []
            with patch(
                "repoach.llm_proxy.providers.claude_code.client.asyncio.create_subprocess_exec",
                side_effect=fake_exec,
            ):
                async for event in provider.stream_response(_request(system_text)):
                    events.append(event)
            return events

        raw_events = asyncio.run(run())
    finally:
        loguru_logger.remove(sink_id)

    parsed: list[dict] = []
    for event in raw_events:
        for line in event.splitlines():
            if line.startswith("data:"):
                parsed.append(json.loads(line[len("data:") :].strip()))
    return parsed


def _sysprompt_path_from_argv(argv: tuple[Any, ...]) -> Path | None:
    """Extract the ``--system-prompt-file`` path from spawn argv, if present."""
    argv_strs = [str(a) for a in argv]
    for i, arg in enumerate(argv_strs):
        if arg == "--system-prompt-file" and i + 1 < len(argv_strs):
            return Path(argv_strs[i + 1])
    return None


# ---------------------------------------------------------------------------
# G1: system prompt travels via file, never argv
# ---------------------------------------------------------------------------


def test_system_prompt_travels_via_file_never_argv() -> None:
    """argv carries ``--system-prompt-file`` and a path; the system prompt
    text appears in NO argv element; the file's bytes equal the system
    prompt at spawn time."""
    system_text = "You are the Planner. Be thorough."
    spawn_log: list[tuple[tuple[Any, ...], _FakeProcess]] = []
    log_records: list[Any] = []
    sysprompt_snapshots: list[bytes] = []

    _collect_sse_and_capture(
        system_text,
        spawn_log,
        log_records,
        sysprompt_snapshots=sysprompt_snapshots,
    )

    assert len(spawn_log) == 1
    argv, _proc = spawn_log[0]
    argv_strs = [str(a) for a in argv]

    assert "--system-prompt-file" in argv_strs, (
        f"Expected --system-prompt-file in argv, got {argv_strs}"
    )

    sysprompt_path = _sysprompt_path_from_argv(argv)
    assert sysprompt_path is not None, "Could not extract sysprompt path from argv"

    assert not any(system_text in a for a in argv_strs), (
        f"System prompt text must not appear in any argv element, got {argv_strs}"
    )

    assert len(sysprompt_snapshots) == 1, (
        f"Expected one sysprompt snapshot, got {len(sysprompt_snapshots)}"
    )
    file_bytes = sysprompt_snapshots[0]
    assert file_bytes.decode("utf-8") == system_text, (
        f"File content {file_bytes!r} != system_text {system_text!r}"
    )


def test_sysprompt_file_removed_after_completion() -> None:
    """After the stream ends, the captured sysprompt file no longer exists on disk."""
    system_text = "You are the Planner. Be thorough."
    spawn_log: list[tuple[tuple[Any, ...], _FakeProcess]] = []
    log_records: list[Any] = []

    _collect_sse_and_capture(system_text, spawn_log, log_records)

    assert len(spawn_log) == 1
    argv, _proc = spawn_log[0]
    sysprompt_path = _sysprompt_path_from_argv(argv)
    assert sysprompt_path is not None

    assert not sysprompt_path.exists(), (
        f"sysprompt file {sysprompt_path} should have been removed after stream, but it still exists"
    )


def test_no_sysprompt_flag_when_absent() -> None:
    """Request with empty system prompt → argv has no ``--system-prompt*``
    token and no file was written under ``self._workdir``."""
    spawn_log: list[tuple[tuple[Any, ...], _FakeProcess]] = []
    log_records: list[Any] = []

    _collect_sse_and_capture("", spawn_log, log_records)

    assert len(spawn_log) == 1
    argv, _proc = spawn_log[0]
    argv_strs = [str(a) for a in argv]

    assert not any("--system-prompt" in a for a in argv_strs), (
        f"argv must not contain any --system-prompt* flag when system prompt is absent, got {argv_strs}"
    )

    sysprompt_path = _sysprompt_path_from_argv(argv)
    assert sysprompt_path is None, (
        f"No sysprompt path should be extractable when system prompt is absent, got {sysprompt_path}"
    )


# ---------------------------------------------------------------------------
# G2: log line reports system_prompt_chars, never the text
# ---------------------------------------------------------------------------


def test_log_line_reports_system_prompt_chars_not_text() -> None:
    """The ``CLAUDE_CODE_STREAM`` log line contains ``system_prompt_chars=<int>``
    and does NOT contain the system prompt text."""
    system_text = "You are the Planner. Be thorough."
    spawn_log: list[tuple[tuple[Any, ...], _FakeProcess]] = []
    log_records: list[Any] = []

    _collect_sse_and_capture(system_text, spawn_log, log_records)

    stream_lines = [r["message"] for r in log_records if "CLAUDE_CODE_STREAM:" in r["message"]]
    assert len(stream_lines) >= 1, (
        f"Expected at least one CLAUDE_CODE_STREAM log line, got {log_records}"
    )

    line = stream_lines[0]
    assert "system_prompt_chars=" in line, (
        f"Log line must contain system_prompt_chars=, got {line!r}"
    )
    assert str(len(system_text)) in line, (
        f"Log line must contain the system prompt char count {len(system_text)}, got {line!r}"
    )
    assert system_text not in line, (
        f"Log line must NOT contain the system prompt text, got {line!r}"
    )


# ---------------------------------------------------------------------------
# G3: loud init warning when cli_path is unresolvable
# ---------------------------------------------------------------------------


def _build_provider_and_capture_loguru(
    cli_path: str,
) -> tuple[ClaudeCodeProvider, list[Any]]:
    """Construct a provider with *cli_path* and capture loguru output.

    Installs a temporary loguru sink that appends each ``Record`` to a
    list, constructs the provider (with ``create_subprocess_exec``
    patched so no real binary is needed), then removes the sink.

    Returns:
        A ``(provider, records)`` pair where *records* is a list of
        loguru ``Record`` dicts captured during construction.
    """
    records: list[Any] = []
    sink_id = loguru_logger.add(
        lambda msg: records.append(msg.record),
        format="{message}",
        level="DEBUG",
    )
    try:
        with patch(
            "repoach.llm_proxy.providers.claude_code.client.asyncio.create_subprocess_exec",
        ):
            provider = ClaudeCodeProvider(
                ProviderConfig(api_key="unused"),
                cli_path=cli_path,
            )
    finally:
        loguru_logger.remove(sink_id)
    return provider, records


def test_which_failure_logs_loud_warning() -> None:
    """Constructing the provider with an unresolvable ``cli_path`` emits the G3 warning."""
    cli_path = "definitely-not-on-path-xyz"
    _provider, records = _build_provider_and_capture_loguru(cli_path)

    warnings = [r for r in records if r["level"].name == "WARNING"]
    assert len(warnings) >= 1, f"Expected at least one WARNING, got records: {records}"

    warning_messages = [r["message"] for r in warnings]
    matching = [
        m for m in warning_messages if "CLAUDE_CODE_CLI_UNRESOLVABLE" in m and cli_path in m
    ]
    assert len(matching) >= 1, (
        f"Expected a WARNING containing CLAUDE_CODE_CLI_UNRESOLVABLE and {cli_path!r}, "
        f"got {warning_messages}"
    )


# ---------------------------------------------------------------------------
# AC2: concurrent requests get distinct sysprompt files
# ---------------------------------------------------------------------------


def test_concurrent_requests_get_distinct_sysprompt_files() -> None:
    """Two concurrent ``stream_response`` calls with distinct system
    prompts must each receive a different ``--system-prompt-file`` path,
    and each file's bytes must equal its own system prompt."""
    system_a = "System prompt Alpha — unique content for A."
    system_b = "System prompt Beta — completely different content for B."

    provider = _provider()
    payload = {"result": "plain answer", "usage": {"output_tokens": 7}}

    spawn_logs: list[tuple[tuple[Any, ...], _FakeProcess]] = []
    sysprompt_snapshots: list[tuple[bytes, Path]] = []

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        proc = _FakeProcess(payload)
        spawn_logs.append((args, proc))
        sp_path = _sysprompt_path_from_argv(args)
        if sp_path is not None and sp_path.exists():
            sysprompt_snapshots.append((sp_path.read_bytes(), sp_path))
        return proc

    async def drive(sys_text: str) -> list[str]:
        events: list[str] = []
        async for event in provider.stream_response(_request(sys_text)):
            events.append(event)
        return events

    async def run_concurrent() -> None:
        with patch(
            "repoach.llm_proxy.providers.claude_code.client.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ):
            await asyncio.gather(drive(system_a), drive(system_b))

    asyncio.run(run_concurrent())

    assert len(spawn_logs) == 2, f"Expected 2 spawns, got {len(spawn_logs)}"
    assert len(sysprompt_snapshots) == 2, (
        f"Expected 2 sysprompt snapshots, got {len(sysprompt_snapshots)}"
    )

    paths = [str(p) for _, p in sysprompt_snapshots]
    assert paths[0] != paths[1], f"Both requests got the same sysprompt file: {paths[0]}"

    file_contents = {content.decode("utf-8") for content, _ in sysprompt_snapshots}
    assert file_contents == {system_a, system_b}, (
        f"File contents {file_contents!r} != expected {{{system_a!r}, {system_b!r}}}"
    )

    for argv, _ in spawn_logs:
        argv_strs = [str(a) for a in argv]
        assert "--system-prompt-file" in argv_strs, (
            f"Expected --system-prompt-file in argv, got {argv_strs}"
        )
        assert not any(system_a in a for a in argv_strs), (
            "System prompt A must not appear in any argv element"
        )
        assert not any(system_b in a for a in argv_strs), (
            "System prompt B must not appear in any argv element"
        )
