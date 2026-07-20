"""Proxy-only AgentLoop smoke tests.

SP-LLM-NATIVE-FORMAT (2026-05-06): :class:`AgentLoop` now speaks
``repoach/v1`` natively — it stores a :class:`CapabilityTier`
(not a model alias chain) and constructs :class:`AgentRequest`
directly.  These tests pin the construction surface:

* default capability is SONNET
* explicit ``capability=`` flows through to ``loop._capability``
* legacy ``model_chain=`` argument resolves to a capability via
  substring classification (back-compat shim)
* empty ``model_chain`` is a programmer error
"""

from unittest.mock import MagicMock, patch

import pytest

from repoach.agent_engine.agent_loop import AgentLoop
from repoach.llm.capability import CapabilityTier


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


def test_agent_loop_routes_to_proxy(mock_settings):
    """The loop wires the proxy URL + token onto its gateway client."""
    loop = AgentLoop(capability=CapabilityTier.SONNET)
    assert loop._base_url == "http://localhost:8082/v1"
    assert loop._api_key == "test-token"
    assert loop._capability is CapabilityTier.SONNET
    assert loop._model_chain == ("claude-sonnet-4-6",)


def test_agent_loop_capability_kwarg_flows_through(mock_settings):
    """Each capability tier wires through to ``loop._capability``."""
    for cap in CapabilityTier:
        loop = AgentLoop(capability=cap)
        assert loop._capability is cap


def test_agent_loop_resolves_each_capability_to_its_alias(mock_settings):
    """The legacy ``_model_chain`` mirror is the matching Anthropic alias."""
    for cap, alias in [
        (CapabilityTier.OPUS, "claude-opus-4-7"),
        (CapabilityTier.SONNET, "claude-sonnet-4-6"),
        (CapabilityTier.HAIKU, "claude-haiku-4-5"),
    ]:
        loop = AgentLoop(capability=cap)
        assert loop._model_chain == (alias,)


def test_agent_loop_legacy_model_chain_classifies_by_substring(mock_settings):
    """Legacy ``model_chain=`` argument resolves to a capability via substring.

    Back-compat shim for callers that haven't migrated to
    ``capability=``.  Each entry's tier keyword decides the
    capability; the chain itself is then collapsed to the single
    capability alias.
    """
    cases = [
        ("claude-opus-4-7", CapabilityTier.OPUS),
        ("CLAUDE-OPUS-anything", CapabilityTier.OPUS),
        ("claude-sonnet-4-6", CapabilityTier.SONNET),
        ("claude-haiku-4-5", CapabilityTier.HAIKU),
        ("claude-coder-repoach", CapabilityTier.SONNET),
        ("garbage-no-tier", CapabilityTier.SONNET),
    ]
    for first_entry, expected in cases:
        loop = AgentLoop(model_chain=(first_entry,))
        assert loop._capability is expected, (
            f"model_chain head {first_entry!r} should resolve to "
            f"{expected.value!r}, got {loop._capability.value!r}"
        )


def test_agent_loop_default_capability_is_sonnet(mock_settings):
    """No ``capability`` and no ``model_chain`` → defaults to SONNET."""
    loop = AgentLoop()
    assert loop._capability is CapabilityTier.SONNET
    assert loop._model_chain == ("claude-sonnet-4-6",)


def test_agent_loop_rejects_empty_model_chain(mock_settings):
    """An explicitly empty chain is a programmer error, not a fallback."""
    with pytest.raises(ValueError, match="model_chain must not be empty"):
        AgentLoop(model_chain=())


def test_agent_loop_rejects_missing_proxy_token(mock_settings):
    """No auth token → loud refusal at construction (proxy auth is required)."""
    mock_settings.llm_proxy_auth_token = _MockSecret("")
    with pytest.raises(ValueError, match="REPOACH_ANTHROPIC_AUTH_TOKEN"):
        AgentLoop()


def test_agent_loop_rejects_none_proxy_token(mock_settings):
    """An unset token must not fall back to any implicit shared secret.

    SP-PROXY-SECURE-DEFAULTS: the historical ``"freecc"`` literal made
    the None case silently authenticate; it must now refuse loudly.
    """
    mock_settings.llm_proxy_auth_token = None
    with pytest.raises(ValueError, match="REPOACH_ANTHROPIC_AUTH_TOKEN"):
        AgentLoop()


# ---------------------------------------------------------------------------
# _extract_text / _extract_tool_calls — direct unit tests for the helpers
# the Reviewer review on PR #102 asked for explicitly.
# ---------------------------------------------------------------------------


def test_extract_text_concatenates_every_text_block(mock_settings):
    """Multiple ``TextBlock`` blocks join in order; ``ToolCallBlock``s are skipped."""
    from repoach.agent_engine.agent_loop import _extract_text
    from repoach.llm_proxy.api.models.agent_v1 import (
        AgentResponse,
        TextBlock,
        ToolCallBlock,
        Usage,
    )

    response = AgentResponse(
        schema_version="1",
        capability="sonnet",
        stop_reason="end_turn",
        model_used="x",
        content=[
            TextBlock(text="hello "),
            ToolCallBlock(id="c1", name="f", args={}),
            TextBlock(text="world"),
        ],
        usage=Usage(input_tokens=0, output_tokens=0, total_tokens=0),
        elapsed_ms=0,
    )
    assert _extract_text(response) == "hello world"


def test_extract_text_returns_empty_for_tool_only_response(mock_settings):
    """A turn that emits only ``ToolCallBlock``s produces an empty text body."""
    from repoach.agent_engine.agent_loop import _extract_text
    from repoach.llm_proxy.api.models.agent_v1 import (
        AgentResponse,
        ToolCallBlock,
        Usage,
    )

    response = AgentResponse(
        schema_version="1",
        capability="sonnet",
        stop_reason="tool_use",
        model_used="x",
        content=[ToolCallBlock(id="c1", name="f", args={})],
        usage=Usage(input_tokens=0, output_tokens=0, total_tokens=0),
        elapsed_ms=0,
    )
    assert _extract_text(response) == ""


def test_extract_tool_calls_filters_blocks(mock_settings):
    """Only ``ToolCallBlock`` instances come through, in declared order."""
    from repoach.agent_engine.agent_loop import _extract_tool_calls
    from repoach.llm_proxy.api.models.agent_v1 import (
        AgentResponse,
        TextBlock,
        ToolCallBlock,
        Usage,
    )

    response = AgentResponse(
        schema_version="1",
        capability="sonnet",
        stop_reason="tool_use",
        model_used="x",
        content=[
            TextBlock(text="thinking"),
            ToolCallBlock(id="c1", name="alpha", args={"a": 1}),
            ToolCallBlock(id="c2", name="beta", args={}),
            TextBlock(text="done"),
        ],
        usage=Usage(input_tokens=0, output_tokens=0, total_tokens=0),
        elapsed_ms=0,
    )
    calls = _extract_tool_calls(response)
    assert [c.name for c in calls] == ["alpha", "beta"]
    assert [c.id for c in calls] == ["c1", "c2"]


# ---------------------------------------------------------------------------
# Error propagation — gateway exceptions surface to the loop's caller
# ---------------------------------------------------------------------------


def test_run_propagates_chain_exhausted_from_client(mock_settings):
    """``GatewayChainExhausted`` from ``_client.call`` propagates unchanged.

    The proxy already exhausted its own ``MODEL_<CAPABILITY>`` chain;
    the loop has no fallback to attempt.  Surfacing the exception
    lets the caller (Reviewer / Coder / etc.) decide how to handle
    a ``stop_reason="error"`` outcome.
    """
    from repoach.agent_engine.adapters import GatewayChainExhausted
    from repoach.agent_engine.agent_loop import AgentLoop

    loop = AgentLoop(capability=CapabilityTier.SONNET)
    with (
        patch.object(
            loop._client,
            "call",
            side_effect=GatewayChainExhausted("every upstream rate-limited"),
        ),
        pytest.raises(GatewayChainExhausted, match="rate-limited"),
    ):
        loop.run_oneshot("hi")


def test_run_propagates_transport_error_from_client(mock_settings):
    """``GatewayTransportError`` (5xx, ConnectError, ReadTimeout) propagates too."""
    from repoach.agent_engine.adapters import GatewayTransportError
    from repoach.agent_engine.agent_loop import AgentLoop

    loop = AgentLoop(capability=CapabilityTier.SONNET)
    with (
        patch.object(
            loop._client,
            "call",
            side_effect=GatewayTransportError("proxy returned 503"),
        ),
        pytest.raises(GatewayTransportError, match="503"),
    ):
        loop.run_oneshot("hi")


# ---------------------------------------------------------------------------
# run() — budget-exhausted wrap-up path (max_turns reached)
# ---------------------------------------------------------------------------


def test_run_wraps_up_when_max_turns_exhausted(mock_settings):
    """When the model keeps calling tools past ``max_turns``, the loop
    forces a final wrap-up call without tools and returns its text.

    Pin: every iteration emits a ``ToolCallBlock`` so the loop never
    naturally exits; on turn ``max_turns + 1`` the wrap-up call
    (no tools) returns the final answer.
    """
    from repoach.agent_engine.agent_loop import AgentLoop, ToolDef
    from repoach.llm_proxy.api.models.agent_v1 import (
        AgentResponse,
        TextBlock,
        ToolCallBlock,
        Usage,
    )

    def _tool_call_response(call_id: str) -> AgentResponse:
        return AgentResponse(
            schema_version="1",
            capability="sonnet",
            stop_reason="tool_use",
            model_used="x",
            content=[ToolCallBlock(id=call_id, name="echo", args={"text": "hi"})],
            usage=Usage(input_tokens=0, output_tokens=0, total_tokens=0),
            elapsed_ms=0,
        )

    wrap_up = AgentResponse(
        schema_version="1",
        capability="sonnet",
        stop_reason="end_turn",
        model_used="x",
        content=[TextBlock(text="forced wrap-up")],
        usage=Usage(input_tokens=0, output_tokens=0, total_tokens=0),
        elapsed_ms=0,
    )

    fake_tool = ToolDef(
        name="echo",
        description="echo",
        parameters_schema={"type": "object"},
        callable_fn=lambda **_kw: {"echoed": "hi"},
    )

    loop = AgentLoop(capability=CapabilityTier.SONNET, max_turns=2)
    # Three calls: turn 1 (tool), turn 2 (tool), wrap-up (text).
    side_effect = [_tool_call_response("c1"), _tool_call_response("c2"), wrap_up]
    with patch.object(loop._client, "call", side_effect=side_effect):
        out = loop.run("go", tools=[fake_tool])

    assert out.text == "forced wrap-up"
    assert out.turns == 2
    assert out.tool_calls_made == ["echo", "echo"]
