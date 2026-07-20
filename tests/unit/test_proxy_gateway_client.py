"""Native ``repoach/v1`` HTTP-client tests for ``ProxyGatewayClient``.

SP-LLM-NATIVE-FORMAT (2026-05-06): the previous file
(``test_proxy_gateway_adapter.py``) tested the OpenAI ⇄
repoach/v1 translation layer that PR #98 added — that
translation has been deleted along with ``ProxyGatewayAdapter``.
The agent loop now constructs :class:`AgentRequest` directly and
reads :class:`AgentResponse` directly; this file pins the new
contract.

Tests cover:

* The wire body the client POSTs (capability tier, system, messages,
  tools, max_tokens, temperature) for happy-path fixtures.
* Each error class the client raises:
  - ``GatewayTransportError`` on 5xx, 429, 408, ConnectError,
    ReadTimeout
  - ``GatewayChainExhausted`` when the proxy returns 200 with
    ``stop_reason="error"`` (its own chain walk failed)
  - ``GatewayError`` on 4xx other than the retryable subset, or
    non-JSON body, or AgentResponse validation failure
* Round-trip fidelity: the response Pydantic model preserves
  every field the agent loop reads (text + tool_call blocks,
  usage, model_used, stop_reason, trace).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from repoach.agent_engine.adapters import (
    GatewayChainExhausted,
    GatewayError,
    GatewayTransportError,
    ProxyGatewayClient,
)
from repoach.llm.capability import CapabilityTier
from repoach.llm_proxy.api.models.agent_v1 import (
    AgentResponse,
    Message,
    TextBlock,
    ToolCallBlock,
    ToolSpec,
)

# ---------------------------------------------------------------------------
# stubs
# ---------------------------------------------------------------------------


class _StubResponse:
    def __init__(
        self,
        *,
        status_code: int,
        payload: Any | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or ""

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class _StubClient:
    """httpx.Client stand-in that records the POST and returns a fixed response."""

    def __init__(self, response: _StubResponse) -> None:
        self._response = response
        self.posted: list[dict[str, Any]] = []

    def __enter__(self) -> _StubClient:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _StubResponse:
        self.posted.append({"url": url, "json": json, "headers": headers})
        return self._response


class _RaisingClient:
    """httpx.Client stand-in that raises a specified exception on ``post``."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.posted: list[dict[str, Any]] = []

    def __enter__(self) -> _RaisingClient:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def post(self, *_args: Any, **_kwargs: Any) -> _StubResponse:
        raise self._exc


def _client() -> ProxyGatewayClient:
    return ProxyGatewayClient(
        base_url="http://localhost:8082",
        api_key="test-token",
        timeout_s=10.0,
    )


def _ok_payload(*, text: str = "PONG", model_used: str = "claude-sonnet-4-6") -> dict[str, Any]:
    return {
        "schema_version": "1",
        "capability": "sonnet",
        "stop_reason": "end_turn",
        "model_used": model_used,
        "content": [{"type": "text", "text": text}] if text else [],
        "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
        "elapsed_ms": 100,
        "trace": [],
    }


# ---------------------------------------------------------------------------
# wire body
# ---------------------------------------------------------------------------


def test_call_posts_agent_request_to_v1_agent(monkeypatch) -> None:
    """The client POSTs to ``/v1/agent`` with the canonical AgentRequest body."""
    stub = _StubClient(_StubResponse(status_code=200, payload=_ok_payload()))
    monkeypatch.setattr(
        "repoach.agent_engine.adapters.httpx.Client",
        lambda **_kw: stub,
    )

    response = _client().call(
        capability=CapabilityTier.SONNET,
        system="you are X",
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        tools=[],
        max_tokens=100,
        temperature=0.1,
    )

    assert isinstance(response, AgentResponse)
    assert len(stub.posted) == 1
    assert stub.posted[0]["url"] == "http://localhost:8082/v1/agent"
    body = stub.posted[0]["json"]
    assert body["schema_version"] == "1"
    assert body["capability"] == "sonnet"
    assert body["system"] == "you are X"
    assert body["max_tokens"] == 100
    assert body["temperature"] == 0.1
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert body["tools"] == []


def test_call_omits_system_field_when_none(monkeypatch) -> None:
    """``system=None`` is dropped from the body (gateway requires absent, not null)."""
    stub = _StubClient(_StubResponse(status_code=200, payload=_ok_payload()))
    monkeypatch.setattr(
        "repoach.agent_engine.adapters.httpx.Client",
        lambda **_kw: stub,
    )
    _client().call(
        capability=CapabilityTier.SONNET,
        system=None,
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        tools=[],
        max_tokens=10,
        temperature=0.1,
    )
    assert "system" not in stub.posted[0]["json"]


def test_call_carries_tools_into_body(monkeypatch) -> None:
    """``ToolSpec`` list is serialised verbatim into ``tools``."""
    stub = _StubClient(_StubResponse(status_code=200, payload=_ok_payload()))
    monkeypatch.setattr(
        "repoach.agent_engine.adapters.httpx.Client",
        lambda **_kw: stub,
    )
    _client().call(
        capability=CapabilityTier.SONNET,
        system=None,
        messages=[Message(role="user", content=[TextBlock(text="go")])],
        tools=[
            ToolSpec(
                name="get_quote",
                description="Fetch a quote.",
                parameters_schema={"type": "object"},
            )
        ],
        max_tokens=50,
        temperature=0.2,
    )
    assert stub.posted[0]["json"]["capability"] == "sonnet"
    assert stub.posted[0]["json"]["tools"] == [
        {
            "name": "get_quote",
            "description": "Fetch a quote.",
            "parameters_schema": {"type": "object"},
        }
    ]


def test_call_sets_both_auth_headers(monkeypatch) -> None:
    """Bearer + X-Api-Key both carry the token (proxy accepts either)."""
    stub = _StubClient(_StubResponse(status_code=200, payload=_ok_payload()))
    monkeypatch.setattr(
        "repoach.agent_engine.adapters.httpx.Client",
        lambda **_kw: stub,
    )
    _client().call(
        capability=CapabilityTier.SONNET,
        system=None,
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        tools=[],
        max_tokens=10,
        temperature=0.1,
    )
    headers = stub.posted[0]["headers"]
    assert headers["Authorization"] == "Bearer test-token"
    assert headers["X-Api-Key"] == "test-token"
    assert headers["Content-Type"] == "application/json"


# ---------------------------------------------------------------------------
# AgentResponse round-trip
# ---------------------------------------------------------------------------


def test_call_returns_pydantic_response_with_text_blocks(monkeypatch) -> None:
    """Successful response parses to AgentResponse with TextBlock content."""
    stub = _StubClient(_StubResponse(status_code=200, payload=_ok_payload(text="PONG")))
    monkeypatch.setattr(
        "repoach.agent_engine.adapters.httpx.Client",
        lambda **_kw: stub,
    )
    response = _client().call(
        capability=CapabilityTier.SONNET,
        system=None,
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        tools=[],
        max_tokens=10,
        temperature=0.1,
    )
    assert response.stop_reason == "end_turn"
    assert response.model_used == "claude-sonnet-4-6"
    assert len(response.content) == 1
    block = response.content[0]
    assert isinstance(block, TextBlock)
    assert block.text == "PONG"
    assert response.usage.total_tokens == 8


def test_call_returns_response_with_tool_call_blocks(monkeypatch) -> None:
    """A tool-using turn surfaces ToolCallBlock entries in content."""
    payload = _ok_payload(text="")
    payload["stop_reason"] = "tool_use"
    payload["content"] = [
        {"type": "text", "text": "calling tool"},
        {
            "type": "tool_call",
            "id": "call_1",
            "name": "get_quote",
            "args": {"match_id": "m-1"},
        },
    ]
    stub = _StubClient(_StubResponse(status_code=200, payload=payload))
    monkeypatch.setattr(
        "repoach.agent_engine.adapters.httpx.Client",
        lambda **_kw: stub,
    )
    response = _client().call(
        capability=CapabilityTier.SONNET,
        system=None,
        messages=[Message(role="user", content=[TextBlock(text="go")])],
        tools=[],
        max_tokens=10,
        temperature=0.1,
    )
    assert response.stop_reason == "tool_use"
    types = [b.type for b in response.content]
    assert types == ["text", "tool_call"]
    tc = response.content[1]
    assert isinstance(tc, ToolCallBlock)
    assert tc.name == "get_quote"
    assert tc.args == {"match_id": "m-1"}


# ---------------------------------------------------------------------------
# error mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [500, 502, 503, 504, 408, 429])
def test_call_raises_transport_error_on_retryable_status(monkeypatch, status: int) -> None:
    """5xx, 408, 429 raise ``GatewayTransportError`` (loop's retry path)."""
    stub = _StubClient(_StubResponse(status_code=status, text="bad"))
    monkeypatch.setattr(
        "repoach.agent_engine.adapters.httpx.Client",
        lambda **_kw: stub,
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


def test_call_raises_transport_error_on_connect_error(monkeypatch) -> None:
    """``httpx.ConnectError`` (proxy not running) is a transport failure."""
    raising = _RaisingClient(httpx.ConnectError("connection refused"))
    monkeypatch.setattr(
        "repoach.agent_engine.adapters.httpx.Client",
        lambda **_kw: raising,
    )
    with pytest.raises(GatewayTransportError, match="proxy unreachable"):
        _client().call(
            capability=CapabilityTier.SONNET,
            system=None,
            messages=[Message(role="user", content=[TextBlock(text="hi")])],
            tools=[],
            max_tokens=10,
            temperature=0.1,
        )


def test_call_raises_transport_error_on_read_timeout(monkeypatch) -> None:
    """``httpx.ReadTimeout`` (proxy hung mid-request) is also transport-level."""
    raising = _RaisingClient(httpx.ReadTimeout("timed out"))
    monkeypatch.setattr(
        "repoach.agent_engine.adapters.httpx.Client",
        lambda **_kw: raising,
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


def test_call_raises_gateway_error_on_non_retryable_4xx(monkeypatch) -> None:
    """A 400 (bad request) raises plain ``GatewayError`` — no retry."""
    stub = _StubClient(_StubResponse(status_code=400, text="bad request"))
    monkeypatch.setattr(
        "repoach.agent_engine.adapters.httpx.Client",
        lambda **_kw: stub,
    )
    with pytest.raises(GatewayError, match="400"):
        _client().call(
            capability=CapabilityTier.SONNET,
            system=None,
            messages=[Message(role="user", content=[TextBlock(text="hi")])],
            tools=[],
            max_tokens=10,
            temperature=0.1,
        )


def test_call_raises_chain_exhausted_on_stop_reason_error(monkeypatch) -> None:
    """200 OK + ``stop_reason="error"`` → ``GatewayChainExhausted`` (not transport)."""
    err_payload = _ok_payload()
    err_payload["stop_reason"] = "error"
    err_payload["model_used"] = None
    err_payload["content"] = []
    err_payload["error"] = {
        "code": "chain_exhausted",
        "message": "every upstream rate-limited",
    }
    stub = _StubClient(_StubResponse(status_code=200, payload=err_payload))
    monkeypatch.setattr(
        "repoach.agent_engine.adapters.httpx.Client",
        lambda **_kw: stub,
    )
    with pytest.raises(GatewayChainExhausted, match="chain_exhausted"):
        _client().call(
            capability=CapabilityTier.SONNET,
            system=None,
            messages=[Message(role="user", content=[TextBlock(text="hi")])],
            tools=[],
            max_tokens=10,
            temperature=0.1,
        )


def test_call_raises_gateway_error_on_validation_failure(monkeypatch) -> None:
    """A 200 with a payload that fails AgentResponse schema → ``GatewayError``."""
    bad_payload = {"this": "is not", "valid": "agent v1"}
    stub = _StubClient(_StubResponse(status_code=200, payload=bad_payload))
    monkeypatch.setattr(
        "repoach.agent_engine.adapters.httpx.Client",
        lambda **_kw: stub,
    )
    with pytest.raises(GatewayError, match="validation"):
        _client().call(
            capability=CapabilityTier.SONNET,
            system=None,
            messages=[Message(role="user", content=[TextBlock(text="hi")])],
            tools=[],
            max_tokens=10,
            temperature=0.1,
        )
