"""SP-PROCESS-REGISTRY-WIRE — the subprocess reaper actually reaps.

Before this fix nothing ever called ``register_pid``, so ``_pids`` stayed
empty and ``kill_all_best_effort`` returned at its ``if not pids`` guard
without touching anything. These tests drive the real
``ClaudeCodeProvider.stream_response`` spawn path with a boundary-fake
``claude`` CLI — a real, short-lived child process standing in for the
slow upstream binary, spawned via the genuine
``asyncio.create_subprocess_exec`` call (never monkeypatched) — and
assert against the real, unpatched ``process_registry`` module.
"""

from __future__ import annotations

import asyncio
import os
import stat
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from repoach.llm_proxy.cli import process_registry
from repoach.llm_proxy.providers.base import ProviderConfig
from repoach.llm_proxy.providers.claude_code.client import ClaudeCodeProvider
from repoach.llm_proxy.providers.exceptions import ProviderError

_FAKE_CLI_SOURCE = """#!/usr/bin/env python3
import json
import os
import sys
import time

with open({pidfile!r}, "w", encoding="utf-8") as fh:
    fh.write(str(os.getpid()))

sys.stdin.read()
time.sleep({sleep})
print(json.dumps({{"result": "ok", "usage": {{"output_tokens": 1}}}}))
"""


@pytest.fixture(autouse=True)
def _clean_process_registry() -> Iterator[None]:
    """Isolate the module-level ``_pids`` singleton across tests."""
    with process_registry._lock:
        process_registry._pids.clear()
    yield
    with process_registry._lock:
        process_registry._pids.clear()


def _write_fake_cli(tmp_path: Path, pidfile: Path, sleep_seconds: float) -> Path:
    script_path = tmp_path / "fake_claude_cli.py"
    script_path.write_text(
        _FAKE_CLI_SOURCE.format(pidfile=str(pidfile), sleep=sleep_seconds),
        encoding="utf-8",
    )
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script_path


def _build_request() -> Any:
    return type(
        "Req",
        (),
        {
            "model": "claude-sonnet-4-6",
            "system": None,
            "messages": [{"role": "user", "content": "hello"}],
            "tools": None,
        },
    )()


def _wait_for_pidfile(pidfile: Path, timeout: float = 5.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pidfile.exists():
            text = pidfile.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        time.sleep(0.01)
    raise AssertionError(f"fake CLI never wrote its pid to {pidfile}")


async def _drain(stream: AsyncIterator[str]) -> None:
    async for _ in stream:
        pass


def test_spawn_registers_pid(tmp_path: Path) -> None:
    """AC1: the spawned PID is present in the registry while the call is live."""
    pidfile = tmp_path / "pid.txt"
    fake_cli = _write_fake_cli(tmp_path, pidfile, sleep_seconds=0.4)
    provider = ClaudeCodeProvider(ProviderConfig(api_key="unused"), cli_path=str(fake_cli))
    request = _build_request()

    observed: dict[str, int] = {}

    async def run() -> None:
        task = asyncio.create_task(_drain(provider.stream_response(request)))
        pid = await asyncio.get_event_loop().run_in_executor(None, _wait_for_pidfile, pidfile)
        observed["pid"] = pid
        await asyncio.sleep(0.05)
        assert pid in process_registry._pids, (
            f"pid {pid} must be registered while the subprocess is running, "
            f"got {process_registry._pids}"
        )
        await task

    asyncio.run(run())
    assert "pid" in observed


def test_clean_exit_deregisters_pid(tmp_path: Path) -> None:
    """AC1: the PID is absent from the registry after clean completion."""
    pidfile = tmp_path / "pid.txt"
    fake_cli = _write_fake_cli(tmp_path, pidfile, sleep_seconds=0.1)
    provider = ClaudeCodeProvider(ProviderConfig(api_key="unused"), cli_path=str(fake_cli))
    request = _build_request()

    async def run() -> int:
        await _drain(provider.stream_response(request))
        return _wait_for_pidfile(pidfile)

    pid = asyncio.run(run())
    assert pid not in process_registry._pids, (
        f"pid {pid} must be deregistered after clean completion, got {process_registry._pids}"
    )
    assert process_registry._pids == set()


def test_kill_all_best_effort_reaps_registered_subprocess(tmp_path: Path) -> None:
    """AC2 (integration): an interrupt reaps the live registered subprocess."""
    pidfile = tmp_path / "pid.txt"
    fake_cli = _write_fake_cli(tmp_path, pidfile, sleep_seconds=5.0)
    provider = ClaudeCodeProvider(ProviderConfig(api_key="unused"), cli_path=str(fake_cli))
    request = _build_request()
    captured_errors: list[BaseException] = []

    async def drain_expect_error() -> None:
        try:
            await _drain(provider.stream_response(request))
        except ProviderError as exc:
            captured_errors.append(exc)

    async def run() -> int:
        task = asyncio.create_task(drain_expect_error())
        pid = await asyncio.get_event_loop().run_in_executor(None, _wait_for_pidfile, pidfile)
        assert pid in process_registry._pids

        process_registry.kill_all_best_effort()
        assert process_registry._pids == set(), (
            "kill_all_best_effort must clear the registry it just reaped"
        )

        await asyncio.wait_for(task, timeout=5.0)
        return pid

    pid = asyncio.run(run())
    assert captured_errors, "the killed subprocess must surface as a provider error"
    assert "-9" in str(captured_errors[0]), (
        f"expected a SIGKILL exit code in the error, got {captured_errors[0]}"
    )

    with pytest.raises(OSError):
        os.kill(pid, 0)
