"""The claude_code provider passes the caller's ``max_tokens`` to the CLI.

``claude --print`` has no output-cap flag and reads
``CLAUDE_CODE_MAX_OUTPUT_TOKENS``; before, the refuter's 800-token
reply cap was exceeded on 5 of 9 calls of Yoob !327 (2026-10-25).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from repoach.llm_proxy.providers.base import ProviderConfig
from repoach.llm_proxy.providers.claude_code.client import OUTPUT_CAP_ENV, ClaudeCodeProvider


class _Process:
    returncode = 0
    pid = 424243

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return json.dumps({"result": "VERDICT: {}", "usage": {"output_tokens": 3}}).encode(), b""


def _spawned_env(request: Any, **provider_kwargs: Any) -> dict[str, str] | None:
    provider = ClaudeCodeProvider(
        ProviderConfig(api_key="unused"), cli_path="claude-stub", **provider_kwargs
    )
    seen: dict[str, Any] = {}

    async def fake_exec(*args: Any, **kwargs: Any) -> _Process:
        seen["env"] = kwargs.get("env")
        return _Process()

    async def run() -> None:
        with patch(
            "repoach.llm_proxy.providers.claude_code.client.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ):
            async for _event in provider.stream_response(request):
                pass

    asyncio.run(run())
    return seen["env"]


def _request(max_tokens: object) -> SimpleNamespace:
    return SimpleNamespace(
        model="claude-opus-4-6",
        system="",
        messages=[{"role": "user", "content": "judge"}],
        tools=None,
        max_tokens=max_tokens,
    )


def test_max_tokens_reaches_the_cli_as_its_output_cap() -> None:
    env = _spawned_env(_request(800))
    assert env is not None
    assert env[OUTPUT_CAP_ENV] == "800"
    assert OUTPUT_CAP_ENV == "CLAUDE_CODE_MAX_OUTPUT_TOKENS"


def test_no_cap_keeps_the_parent_environment() -> None:
    assert _spawned_env(_request(None)) is None
    assert _spawned_env(_request(True)) is None


def test_cap_rides_on_the_filtered_environment() -> None:
    env = _spawned_env(_request(1500), child_env=lambda _parent: {"PATH": "/usr/bin"})
    assert env == {"PATH": "/usr/bin", OUTPUT_CAP_ENV: "1500"}
