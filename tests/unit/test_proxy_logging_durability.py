"""Unit tests for SP-NIM-LOG-DURABILITY (logging side).

The proxy used to truncate ``server.log`` on every restart, so NIM
telemetry never accumulated across sessions. These tests pin the new
behaviour — a reconfigure appends rather than wipes — while fully
restoring the global loguru + stdlib logging state so they cannot
pollute sibling tests (see the test-pollution lesson).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from loguru import logger as loguru_logger

import repoach.llm_proxy.config.logging_config as logging_config


@pytest.fixture()
def restore_global_logging() -> Iterator[None]:
    saved_handlers = logging.root.handlers[:]
    saved_level = logging.root.level
    saved_configured = logging_config._configured
    try:
        yield
    finally:
        loguru_logger.remove()
        logging.root.handlers = saved_handlers
        logging.root.setLevel(saved_level)
        logging_config._configured = saved_configured


def test_configure_logging_appends_not_truncates(
    tmp_path: Path, restore_global_logging: None
) -> None:
    log_file = tmp_path / "logs" / "server.log"
    log_file.parent.mkdir(parents=True)
    log_file.write_text('{"event": "before_restart"}\n')

    logging_config.configure_logging(str(log_file), force=True)
    loguru_logger.info("after_restart_marker")

    content = log_file.read_text()
    assert "before_restart" in content
    assert "after_restart_marker" in content


def test_configure_logging_creates_missing_log_dir(
    tmp_path: Path, restore_global_logging: None
) -> None:
    log_file = tmp_path / "fresh" / "server.log"

    logging_config.configure_logging(str(log_file), force=True)
    loguru_logger.info("first_line")

    assert log_file.exists()
    assert "first_line" in log_file.read_text()
