"""SP-CC-SUBPROCESS-KILL-ON-CANCEL — the ``claude`` CLI child is reaped on every exit path.

Before this fix, an ``asyncio.CancelledError`` / ``GeneratorExit`` reaching
``ClaudeCodeProvider.stream_response``'s ``finally`` block (upstream task
cancellation, or the provider's own ``subprocess_timeout`` firing) removed
the spawned PID from :mod:`process_registry`'s atexit safety net without
ever sending a signal to the process it named — an orphaned ``claude``
child kept running, unsupervised, burning Max-plan quota and CPU.

These tests drive the real spawn path with a boundary-fake ``claude`` CLI
— a real, short-lived Python child process standing in for the slow
upstream binary, spawned via the genuine ``asyncio.create_subprocess_exec``
call (never monkeypatched for AC1/AC2) — and assert against the real OS
process table via ``os.kill(pid, 0)``.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

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


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as exc:
        logger.debug("pid {} liveness probe raised {}", pid, exc)
        return False
    return True


async def _drain(stream: AsyncIterator[str]) -> None:
    async for _ in stream:
        pass


def test_stream_task_cancellation_kills_subprocess(tmp_path: Path) -> None:
    """AC1/AC4: cancelling the driving task mid-``communicate`` kills the child.

    A long-sleeping fake CLI is spawned for real; once its PID is known
    the driving task is cancelled while ``proc.communicate()`` is still
    in flight (an upstream cancellation, e.g. a client disconnect or a
    higher-layer ``asyncio.wait_for`` firing). The OS process must be
    dead within the SIGTERM grace period plus a small buffer — not
    merely absent from ``process_registry._pids``.
    """
    pidfile = tmp_path / "pid.txt"
    fake_cli = _write_fake_cli(tmp_path, pidfile, sleep_seconds=5.0)
    provider = ClaudeCodeProvider(ProviderConfig(api_key="unused"), cli_path=str(fake_cli))
    request = _build_request()

    async def run() -> int:
        task = asyncio.create_task(_drain(provider.stream_response(request)))
        pid = await asyncio.get_event_loop().run_in_executor(None, _wait_for_pidfile, pidfile)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return pid

    pid = asyncio.run(run())

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and _pid_is_alive(pid):
        time.sleep(0.05)

    assert not _pid_is_alive(pid), (
        f"pid {pid} must be dead after the driving task was cancelled mid-communicate"
    )


def test_internal_timeout_kills_subprocess(tmp_path: Path) -> None:
    """AC2/AC4: the provider's own ``subprocess_timeout`` also kills the child.

    No external cancellation is involved: the fake CLI sleeps longer
    than a small injected ``subprocess_timeout``, so the provider's own
    ``asyncio.wait_for(proc.communicate(...), ...)`` times out and
    raises ``ProviderError``. The OS process must be dead after the
    call raises.
    """
    pidfile = tmp_path / "pid.txt"
    fake_cli = _write_fake_cli(tmp_path, pidfile, sleep_seconds=5.0)
    provider = ClaudeCodeProvider(
        ProviderConfig(api_key="unused"),
        cli_path=str(fake_cli),
        subprocess_timeout=0.2,
    )
    request = _build_request()

    async def run() -> int:
        with pytest.raises(ProviderError):
            await _drain(provider.stream_response(request))
        return _wait_for_pidfile(pidfile)

    pid = asyncio.run(run())

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and _pid_is_alive(pid):
        time.sleep(0.05)

    assert not _pid_is_alive(pid), (
        f"pid {pid} must be dead after the call raised ProviderError on its own timeout"
    )


class _FakeCompletedProcess:
    """Stub subprocess that has already completed by the time ``communicate`` returns.

    ``terminate`` / ``kill`` are ``MagicMock`` spies so the happy path
    can assert neither is ever invoked.
    """

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.returncode = 0
        self.pid = 999999
        self.terminate = MagicMock()
        self.kill = MagicMock()

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return json.dumps(self._payload).encode(), b""

    async def wait(self) -> int:
        return self.returncode


def test_clean_completion_never_signals_subprocess() -> None:
    """AC3/AC4: a normal completion sends no signal to the child.

    The fake CLI exits immediately with a valid payload
    (``proc.returncode`` already ``0`` by the time ``communicate()``
    returns) — ``terminate``/``kill`` must never be called on the
    happy path, and the stream must complete without raising.
    """
    provider = ClaudeCodeProvider(ProviderConfig(api_key="unused"), cli_path="claude-stub")
    request = _build_request()
    payload = {"result": "all good", "usage": {"output_tokens": 3}}
    fake_procs: list[_FakeCompletedProcess] = []

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeCompletedProcess:
        proc = _FakeCompletedProcess(payload)
        fake_procs.append(proc)
        return proc

    async def run() -> None:
        with patch(
            "repoach.llm_proxy.providers.claude_code.client.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ):
            await _drain(provider.stream_response(request))

    asyncio.run(run())

    assert len(fake_procs) == 1
    fake_procs[0].terminate.assert_not_called()
    fake_procs[0].kill.assert_not_called()
