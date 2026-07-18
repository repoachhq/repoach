"""SP-CC-SYSPROMPT-FILE — G3: loud init warning when cli_path is unresolvable.

Tests that :class:`ClaudeCodeProvider.__init__` emits a loud warning
when ``shutil.which`` cannot resolve the requested ``cli_path``, so the
next boot-PATH regression is diagnosable from the log head instead of
per-call 500 forensics.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from loguru import logger as loguru_logger

from ferova.llm_proxy.providers.base import ProviderConfig
from ferova.llm_proxy.providers.claude_code.client import ClaudeCodeProvider


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
            "ferova.llm_proxy.providers.claude_code.client.asyncio.create_subprocess_exec",
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
