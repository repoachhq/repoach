"""Unit tests for SP-REVIEWER-TRACE-FIELD plumbing in ``AgentLoop``.

Pins that :func:`_trace_to_dicts` flattens proxy
:class:`TraceEntry` rows into the four operator-actionable fields
(``attempt`` / ``model`` / ``outcome`` / ``elapsed_ms``) plus a
truncated ``error_message`` for non-success outcomes, and that
:meth:`AgentLoop.run_oneshot` propagates that trace onto the
returned :class:`NimAgentOutput`.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from repoach.agent_engine.agent_loop import (
    AgentLoop,
    _trace_to_dicts,
)
from repoach.llm.capability import CapabilityTier
from repoach.llm_proxy.api.models.agent_v1 import (
    AgentResponse,
    TextBlock,
    TraceEntry,
    Usage,
)


class _MockSecret:
    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


@pytest.fixture
def mock_settings():
    with patch("repoach.agent_engine.agent_loop.get_settings") as mock:
        settings = MagicMock()
        settings.llm_proxy_base_url = "http://localhost:8082"
        settings.llm_proxy_auth_token = _MockSecret("test-token")
        mock.return_value = settings
        yield settings


def _trace_entry(*, model: str, outcome: str, elapsed_ms: int, error: str | None) -> TraceEntry:
    return TraceEntry(
        model=model,
        outcome=outcome,
        elapsed_ms=elapsed_ms,
        usage=None,
        error_message=error,
    )


def test_trace_to_dicts_flattens_each_entry_with_attempt_ordinal() -> None:
    """Every TraceEntry becomes a dict with attempt numbering preserved."""
    trace = [
        _trace_entry(model="nim/a", outcome="transport_error", elapsed_ms=120, error="boom"),
        _trace_entry(model="nim/b", outcome="ok", elapsed_ms=340, error=None),
    ]

    out = _trace_to_dicts(trace)

    assert len(out) == 2
    assert out[0] == {
        "attempt": 1,
        "model": "nim/a",
        "outcome": "transport_error",
        "elapsed_ms": 120,
        "error_message": "boom",
    }
    assert out[1] == {
        "attempt": 2,
        "model": "nim/b",
        "outcome": "ok",
        "elapsed_ms": 340,
        "error_message": None,
    }


def test_trace_to_dicts_truncates_long_error_messages() -> None:
    """Error messages over 200 chars are truncated to avoid bloating JSON artifacts."""
    long_error = "x" * 500
    trace = [
        _trace_entry(model="nim/a", outcome="transport_error", elapsed_ms=10, error=long_error)
    ]

    out = _trace_to_dicts(trace)

    assert len(out[0]["error_message"]) == 200
    assert out[0]["error_message"] == "x" * 200


def test_trace_to_dicts_empty_input_yields_empty_list() -> None:
    """An empty trace round-trips to an empty list (no crash, no sentinel entry)."""
    assert _trace_to_dicts([]) == []


def test_run_oneshot_propagates_trace_onto_output(mock_settings) -> None:
    """``AgentResponse.trace`` flows through to ``NimAgentOutput.trace``."""
    loop = AgentLoop(capability=CapabilityTier.SONNET)
    response = AgentResponse(
        capability="sonnet",
        stop_reason="end_turn",
        model_used="nim/sonnet-final",
        content=[TextBlock(text="hello")],
        usage=Usage(input_tokens=10, output_tokens=20, total_tokens=30),
        elapsed_ms=500,
        trace=[
            _trace_entry(model="nim/a", outcome="transport_error", elapsed_ms=80, error="oops"),
            _trace_entry(model="nim/sonnet-final", outcome="ok", elapsed_ms=420, error=None),
        ],
    )

    with patch.object(loop._client, "call", return_value=response) as call_mock:
        out = loop.run_oneshot("hi")

    call_mock.assert_called_once()
    assert out.text == "hello"
    assert out.model_used == "nim/sonnet-final"
    assert out.trace == [
        {
            "attempt": 1,
            "model": "nim/a",
            "outcome": "transport_error",
            "elapsed_ms": 80,
            "error_message": "oops",
        },
        {
            "attempt": 2,
            "model": "nim/sonnet-final",
            "outcome": "ok",
            "elapsed_ms": 420,
            "error_message": None,
        },
    ]


def test_run_oneshot_empty_proxy_trace_yields_empty_output_trace(mock_settings) -> None:
    """Older proxy versions returning no trace still produce a clean empty list."""
    loop = AgentLoop(capability=CapabilityTier.SONNET)
    response = AgentResponse(
        capability="sonnet",
        stop_reason="end_turn",
        model_used="nim/sonnet-final",
        content=[TextBlock(text="hi")],
        usage=Usage(input_tokens=5, output_tokens=5, total_tokens=10),
        elapsed_ms=100,
        trace=[],
    )

    with patch.object(loop._client, "call", return_value=response):
        out = loop.run_oneshot("hi")

    assert out.trace == []
