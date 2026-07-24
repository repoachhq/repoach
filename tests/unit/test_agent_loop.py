"""Unit tests for SP-CONFIG-ENV-ANCHOR's ``AgentLoop`` half (G3).

``agent_loop.py:332`` used to duplicate the retired ``:8082`` literal as
an ``or`` fallback — a second hardcoded default surviving any future fix
to ``Settings``. Pins that the call site now reads
``settings.llm_proxy_base_url`` verbatim, with no fallback of its own.
"""

from __future__ import annotations

from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from repoach.agent_engine.agent_loop import (
    _MAX_TOOL_ARG_BYTES,
    AgentLoop,
    ToolDef,
    _validate_tool_args,
)
from repoach.llm.capability import CapabilityTier
from repoach.llm_proxy.api.models.agent_v1 import (
    AgentResponse,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    ToolResultError,
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
        settings.llm_proxy_base_url = "http://sentinel-host:9999"
        settings.llm_proxy_auth_token = _MockSecret("test-token")
        mock.return_value = settings
        yield settings


def test_base_url_has_no_hardcoded_fallback(mock_settings) -> None:
    """The client targets whatever ``settings.llm_proxy_base_url`` says.

    A sentinel host that has nothing to do with either ``:8082`` or
    ``:8084`` proves the call site reads the resolved value verbatim
    rather than silently substituting a literal of its own.
    """
    loop = AgentLoop(capability=CapabilityTier.SONNET)
    assert loop._client._base_url == "http://sentinel-host:9999"


def test_base_url_never_falls_back_to_the_retired_8082_default(mock_settings) -> None:
    """A falsy resolved URL must NOT be coerced to the old ``:8082`` literal.

    Pre-fix: ``(settings.llm_proxy_base_url or "http://localhost:8082")``
    turned any falsy value into the hardcoded default. Post-fix there is
    no ``or`` fallback left at the call site, so a falsy value passes
    through unchanged instead of silently becoming ``:8082``.
    """
    mock_settings.llm_proxy_base_url = ""
    loop = AgentLoop(capability=CapabilityTier.SONNET)
    assert "8082" not in loop._client._base_url
    assert loop._client._base_url == ""


class TestValidateToolArgs:
    """AC1 — ``_validate_tool_args`` return value per rejection case.

    SP-TOOL-DISPATCH-VALIDATE (audit 2026-07-13 finding M16): the model
    is an untrusted chain participant, so its tool-call args are
    validated against the tool's declared ``parameters_schema`` before
    ever reaching a callable.
    """

    _SCHEMA: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "count": {"type": "number"},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def test_matching_args_pass(self) -> None:
        reason = _validate_tool_args(
            self._SCHEMA, {"path": "a.py", "count": 3}, max_bytes=_MAX_TOOL_ARG_BYTES
        )
        assert reason is None

    def test_extra_unknown_property_is_rejected(self) -> None:
        reason = _validate_tool_args(
            self._SCHEMA,
            {"path": "a.py", "unexpected": "x"},
            max_bytes=_MAX_TOOL_ARG_BYTES,
        )
        assert reason is not None
        assert "unexpected" in reason

    def test_missing_required_key_is_rejected(self) -> None:
        reason = _validate_tool_args(self._SCHEMA, {"count": 1}, max_bytes=_MAX_TOOL_ARG_BYTES)
        assert reason is not None
        assert "path" in reason

    def test_wrong_json_type_is_rejected(self) -> None:
        reason = _validate_tool_args(
            self._SCHEMA,
            {"path": "a.py", "count": "not-a-number"},
            max_bytes=_MAX_TOOL_ARG_BYTES,
        )
        assert reason is not None
        assert "count" in reason

    def test_non_dict_args_are_rejected(self) -> None:
        reason = _validate_tool_args(self._SCHEMA, ["a.py"], max_bytes=_MAX_TOOL_ARG_BYTES)
        assert reason is not None

    def test_oversized_args_are_rejected(self) -> None:
        reason = _validate_tool_args(
            {"type": "object", "properties": {"path": {"type": "string"}}},
            {"path": "x" * (_MAX_TOOL_ARG_BYTES + 1)},
            max_bytes=_MAX_TOOL_ARG_BYTES,
        )
        assert reason is not None


class _ScriptedGatewayClient:
    """Truthful boundary fake for the proxy transport (SP-TOOL-DISPATCH-VALIDATE).

    Returns each scripted :class:`AgentResponse` in order on successive
    ``call`` invocations and records the kwargs of every call, so tests
    can assert what got fed back to the model on the next turn.
    """

    def __init__(self, responses: list[AgentResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


def _tool_call_response(args: dict) -> AgentResponse:
    return AgentResponse(
        schema_version="1",
        capability="sonnet",
        stop_reason="tool_use",
        model_used="fake/sonnet",
        content=[ToolCallBlock(id="call-1", name="write_file", args=args)],
        usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        elapsed_ms=10,
        trace=[],
    )


def _final_text_response(text: str = "done") -> AgentResponse:
    return AgentResponse(
        schema_version="1",
        capability="sonnet",
        stop_reason="end_turn",
        model_used="fake/sonnet",
        content=[TextBlock(text=text)],
        usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        elapsed_ms=10,
        trace=[],
    )


def _write_file_tool(recorder: list[dict]) -> ToolDef:
    def callable_fn(**kwargs):
        recorder.append(kwargs)
        return "written"

    return ToolDef(
        name="write_file",
        description="Write a file.",
        parameters_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "count": {"type": "number"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        callable_fn=callable_fn,
    )


class TestToolDispatchValidation:
    """AC2/AC3 — the real loop rejects schema-violating tool calls at dispatch.

    Drives :meth:`AgentLoop.run` with a scripted gateway client (a
    truthful boundary fake for the proxy transport) that returns a
    real ``tool_call`` block with args violating the tool's declared
    schema, then a final text turn. Observes dispatch behaviour, not
    just the helper's return value.
    """

    def test_schema_violating_args_rejected_before_dispatch(self, mock_settings) -> None:
        invocations: list[dict] = []
        client = _ScriptedGatewayClient(
            [
                _tool_call_response({"path": "a.py", "count": "not-a-number"}),
                _final_text_response(),
            ]
        )
        loop = AgentLoop(capability=CapabilityTier.SONNET)
        loop._client = client

        output = loop.run("go", tools=[_write_file_tool(invocations)])

        assert output.text == "done"
        assert invocations == []
        assert len(client.calls) == 2
        fed_back_message = client.calls[1]["messages"][-1]
        assert fed_back_message.role == "user"
        result_block = fed_back_message.content[0]
        assert isinstance(result_block, ToolResultBlock)
        assert isinstance(result_block.result, ToolResultError)
        assert result_block.result.ok is False
        assert "count" in result_block.result.error["message"]

    def test_oversized_args_rejected(self, mock_settings) -> None:
        invocations: list[dict] = []
        oversized_path = "x" * (_MAX_TOOL_ARG_BYTES + 1)
        client = _ScriptedGatewayClient(
            [
                _tool_call_response({"path": oversized_path}),
                _final_text_response(),
            ]
        )
        loop = AgentLoop(capability=CapabilityTier.SONNET)
        loop._client = client

        output = loop.run("go", tools=[_write_file_tool(invocations)])

        assert output.text == "done"
        assert invocations == []
        fed_back_message = client.calls[1]["messages"][-1]
        result_block = fed_back_message.content[0]
        assert isinstance(result_block.result, ToolResultError)
        assert result_block.result.ok is False

    def test_valid_args_still_dispatched(self, mock_settings) -> None:
        invocations: list[dict] = []
        client = _ScriptedGatewayClient(
            [
                _tool_call_response({"path": "a.py", "count": 3}),
                _final_text_response(),
            ]
        )
        loop = AgentLoop(capability=CapabilityTier.SONNET)
        loop._client = client

        output = loop.run("go", tools=[_write_file_tool(invocations)])

        assert output.text == "done"
        assert invocations == [{"path": "a.py", "count": 3}]
        fed_back_message = client.calls[1]["messages"][-1]
        result_block = fed_back_message.content[0]
        assert result_block.result.ok is True
