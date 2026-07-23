"""AC3 (SP-PROXY-LOG-CONTENT-GUARD) — ``claude`` CLI system prompt off argv.

Pins that the ``claude_code`` command builder places the system
prompt on a file (never argv) and that the ``CLAUDE_CODE_STREAM``
command-log record contains no system-prompt substring. G3/G4 of this
spec were already delivered by the prior SP-CC-SYSPROMPT-FILE fix
(``tests/unit/test_claude_code_sysprompt_file.py``); this module adds
the exact selector this spec's Acceptance Criteria promise, driven
independently of that suite.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

from loguru import logger as loguru_logger

from repoach.llm_proxy.providers.base import ProviderConfig
from repoach.llm_proxy.providers.claude_code.client import ClaudeCodeProvider

_SYSTEM_PROMPT = "You are a code reviewer. Be terse and specific."


class _FakeProcess:
    """Boundary fake standing in for ``asyncio.create_subprocess_exec``."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.returncode = 0

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return json.dumps(self._payload).encode(), b""


def test_system_prompt_off_argv() -> None:
    """Spawn argv never contains the system prompt text, and the
    ``CLAUDE_CODE_STREAM`` log record contains no substring of it."""
    provider = ClaudeCodeProvider(ProviderConfig(api_key="unused"), cli_path="claude-stub")
    request = type(
        "Req",
        (),
        {
            "model": "claude-sonnet-4-6",
            "system": _SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": "Review this diff."}],
            "tools": None,
        },
    )()

    spawn_argv: list[tuple[Any, ...]] = []
    log_records: list[Any] = []

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        spawn_argv.append(args)
        return _FakeProcess({"result": "looks fine", "usage": {"output_tokens": 3}})

    sink_id = loguru_logger.add(
        lambda msg: log_records.append(msg.record),
        format="{message}",
        level="DEBUG",
    )
    try:

        async def run() -> None:
            with patch(
                "repoach.llm_proxy.providers.claude_code.client.asyncio.create_subprocess_exec",
                side_effect=fake_exec,
            ):
                async for _event in provider.stream_response(request):
                    pass

        asyncio.run(run())
    finally:
        loguru_logger.remove(sink_id)

    assert len(spawn_argv) == 1
    argv_strs = [str(a) for a in spawn_argv[0]]
    assert not any(_SYSTEM_PROMPT in a for a in argv_strs), (
        f"system prompt must never appear in spawn argv, got {argv_strs}"
    )

    stream_lines = [r["message"] for r in log_records if "CLAUDE_CODE_STREAM:" in r["message"]]
    assert stream_lines, f"expected at least one CLAUDE_CODE_STREAM log line, got {log_records}"
    assert not any(_SYSTEM_PROMPT in line for line in stream_lines), (
        f"CLAUDE_CODE_STREAM log must not contain the system prompt text, got {stream_lines}"
    )
