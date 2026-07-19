"""Unit tests for SP-PROXY-LOG-EXTRA (extra-field serialization).

The JSON sink used to emit only the six fixed fields plus the three
whitelisted context keys, silently dropping every kwarg-style extra —
the whole chain-walk telemetry arc was writing attribute-less events.
These tests pin the new behaviour: all extras land at top level,
context keys keep precedence, reserved-name collisions are prefixed
with ``extra_`` instead of overwriting record fields, and
non-serialisable values degrade via ``str`` without raising. Global
loguru + stdlib logging state is fully restored after each test (see
the test-pollution lesson).
"""

from __future__ import annotations

import json
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


def _last_json_line(log_file: Path) -> dict:
    lines = [line for line in log_file.read_text().splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_extra_kwargs_serialized(tmp_path: Path, restore_global_logging: None) -> None:
    log_file = tmp_path / "server.log"
    logging_config.configure_logging(str(log_file), force=True)

    loguru_logger.bind(request_id="req_x").warning(
        "proxy_chain_failover_fired",
        primary_reason="empty_completion",
        latency_s=1.25,
        chain_remaining=3,
    )

    payload = _last_json_line(log_file)
    assert payload["message"] == "proxy_chain_failover_fired"
    assert payload["request_id"] == "req_x"
    assert payload["primary_reason"] == "empty_completion"
    assert payload["latency_s"] == 1.25
    assert payload["chain_remaining"] == 3
    assert payload["level"] == "WARNING"


def test_context_keys_promoted(tmp_path: Path, restore_global_logging: None) -> None:
    log_file = tmp_path / "server.log"
    logging_config.configure_logging(str(log_file), force=True)

    with loguru_logger.contextualize(request_id="req_1", node_id="node_2", chat_id=None):
        loguru_logger.info("proxy_chain_exhausted", failures=4)

    payload = _last_json_line(log_file)
    assert payload["request_id"] == "req_1"
    assert payload["node_id"] == "node_2"
    assert "chat_id" not in payload
    assert payload["failures"] == 4


def test_reserved_key_collision_prefixed(tmp_path: Path, restore_global_logging: None) -> None:
    log_file = tmp_path / "server.log"
    logging_config.configure_logging(str(log_file), force=True)

    loguru_logger.info("genuine_event", message="impostor", line=999)

    payload = _last_json_line(log_file)
    assert payload["message"] == "genuine_event"
    assert payload["extra_message"] == "impostor"
    assert payload["line"] != 999
    assert payload["extra_line"] == 999


def test_non_serializable_value_degrades(tmp_path: Path, restore_global_logging: None) -> None:
    log_file = tmp_path / "server.log"
    logging_config.configure_logging(str(log_file), force=True)

    loguru_logger.info("proxy_budget_retry", target_path=Path("/tmp/x"), exc=ValueError("boom"))

    payload = _last_json_line(log_file)
    assert payload["target_path"] == "/tmp/x"
    assert "boom" in payload["exc"]
