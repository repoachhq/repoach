"""SP-ADAPTER-TIMEOUT-RETRY — the httpx timeout family is retryable.

Audit 2026-07-13 finding H9: ``ProxyGatewayClient.call`` only caught
``httpx.ConnectError`` and ``httpx.ReadTimeout``. ``ConnectTimeout`` is
NOT a subclass of ``ConnectError`` (it subclasses ``TimeoutException``
instead), so a connect stall escaped the two-class catch, propagated as
a bare ``httpx.ConnectTimeout``, and killed the whole agent session
instead of being retried like every other transport fault.

These tests drive the real ``ProxyGatewayClient`` (and, for the retry
path, the real ``AgentLoop``) against an ``httpx.MockTransport`` whose
handler raises genuine httpx exception instances — the only fake here
is the transport boundary itself, never Repoach's own classification
logic.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from pydantic import SecretStr

from repoach.agent_engine.adapters import GatewayTransportError, ProxyGatewayClient
from repoach.agent_engine.agent_loop import AgentLoop, ToolDef
from repoach.llm.capability import CapabilityTier
from repoach.llm_proxy.api.models.agent_v1 import Message, TextBlock

_TIMEOUT_FAMILY = pytest.mark.parametrize(
    "make_exc",
    [
        lambda: httpx.ConnectTimeout("connect stalled"),
        lambda: httpx.PoolTimeout("pool exhausted"),
        lambda: httpx.WriteTimeout("write stalled"),
        lambda: httpx.ReadTimeout("read stalled"),
        lambda: httpx.RemoteProtocolError("server closed mid-response"),
    ],
    ids=["ConnectTimeout", "PoolTimeout", "WriteTimeout", "ReadTimeout", "RemoteProtocolError"],
)


def _client() -> ProxyGatewayClient:
    return ProxyGatewayClient(
        base_url="http://localhost:8082",
        api_key="test-token",
        timeout_s=10.0,
    )


_RealHttpxClient = httpx.Client


def _raising_httpx_client_factory(exc: Exception):
    def _handler(_request: httpx.Request) -> httpx.Response:
        raise exc

    def _make_client(**_kwargs: object) -> httpx.Client:
        return _RealHttpxClient(transport=httpx.MockTransport(_handler))

    return _make_client


@_TIMEOUT_FAMILY
def test_connect_timeout_is_gateway_transport_error(monkeypatch, make_exc) -> None:
    """Every timeout-family / transport-fault exception maps to GatewayTransportError."""
    exc = make_exc()
    monkeypatch.setattr(
        "repoach.agent_engine.adapters.httpx.Client",
        _raising_httpx_client_factory(exc),
    )

    with pytest.raises(GatewayTransportError):
        _client().call(
            capability=CapabilityTier.SONNET,
            system=None,
            messages=[Message(role="user", content=[TextBlock(text="hi")])],
            tools=[],
            max_tokens=10,
            temperature=0.1,
        )


@pytest.fixture(autouse=True)
def _stub_proxy_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``AgentLoop`` constructible without the operator ``.env``."""
    monkeypatch.setattr(
        "repoach.agent_engine.agent_loop.get_settings",
        lambda: SimpleNamespace(
            llm_proxy_base_url="http://localhost:8082",
            llm_proxy_auth_token=SecretStr("test-token"),
        ),
    )


def _tool() -> ToolDef:
    return ToolDef(
        name="noop",
        description="never called",
        parameters_schema={"type": "object", "properties": {}},
        callable_fn=lambda **kwargs: "ok",
    )


def _ok_response_payload() -> dict[str, object]:
    return {
        "schema_version": "1",
        "capability": "sonnet",
        "stop_reason": "end_turn",
        "model_used": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": "done"}],
        "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
        "elapsed_ms": 100,
        "trace": [],
    }


def test_agent_loop_retries_connect_timeout(monkeypatch) -> None:
    """A real AgentLoop over a real ProxyGatewayClient survives a timed-out hop.

    The MockTransport raises ``httpx.ConnectTimeout`` on the first two
    calls, then answers with a valid turn — the timeout must be
    treated exactly like the pre-existing ``ConnectError`` /
    ``ReadTimeout`` retry path, not abort the session.
    """
    calls = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.ConnectTimeout("connect stalled", request=request)
        return httpx.Response(200, json=_ok_response_payload())

    def _make_client(**_kwargs: object) -> httpx.Client:
        return _RealHttpxClient(transport=httpx.MockTransport(_handler))

    monkeypatch.setattr("repoach.agent_engine.adapters.httpx.Client", _make_client)

    loop = AgentLoop(model_chain=("claude-sonnet-4-6",))
    with patch.object(AgentLoop, "_TURN_RETRY_BACKOFFS_S", (0.0, 0.0, 0.0)):
        output = loop.run("go", tools=[_tool()])

    assert output.text == "done"
    assert calls["n"] == 3
