"""Tests for the optional ``thinking`` field on ``AgentRequest``.

Part of SP-AGENT-THINKING-CONTROL step 1/4 — the schema alone must
accept an explicit ``ThinkingConfig`` value and default it to ``None``
for the many existing callers that build ``AgentRequest(...)``
without one, with ``model_dump(exclude_none=True)`` round-tripping
the field cleanly so the dispatcher can copy it verbatim onto the
translated ``MessagesRequest``.
"""

from __future__ import annotations

from typing import Any

from ferova.agent_engine.adapters import ProxyGatewayClient
from ferova.llm.capability import CapabilityTier
from ferova.llm_proxy.api.models.agent_v1 import AgentRequest, Message, TextBlock
from ferova.llm_proxy.api.models.anthropic import ThinkingConfig


def _build_request(*, thinking: ThinkingConfig | None = None) -> AgentRequest:
    return AgentRequest(
        schema_version="1",
        capability="sonnet",
        system="You are Ferova's WhatsApp assistant.",
        messages=[Message(role="user", content=[TextBlock(type="text", text="ping")])],
        tools=[],
        thinking=thinking,
    )


def test_agent_request_accepts_thinking_field() -> None:
    """An enabled thinking config round-trips through ``model_dump``.

    The dispatcher copies the field verbatim onto the built
    ``MessagesRequest``; the dump must therefore carry the same
    ``type`` and ``budget_tokens`` the caller supplied.
    """
    thinking = ThinkingConfig(type="enabled", budget_tokens=1024)
    request = _build_request(thinking=thinking)

    assert request.thinking is not None
    assert request.thinking.type == "enabled"
    assert request.thinking.budget_tokens == 1024

    dumped = request.model_dump(exclude_none=True)
    assert "thinking" in dumped
    assert dumped["thinking"]["type"] == "enabled"
    assert dumped["thinking"]["budget_tokens"] == 1024


def test_agent_request_thinking_defaults_to_none() -> None:
    """Existing callers building ``AgentRequest`` without the field keep working.

    ``model_dump(exclude_none=True)`` must omit the field entirely so
    the dispatcher sees no thinking config and falls back to today's
    behaviour (provider global default).
    """
    request = _build_request()

    assert request.thinking is None

    dumped = request.model_dump(exclude_none=True)
    assert "thinking" not in dumped


def test_agent_request_disabled_thinking_round_trips() -> None:
    """A ``disabled`` thinking config survives the dump intact.

    Providers that support an off-switch disable reasoning; others
    strip reasoning output client-side. Either way the dispatcher
    must hand the value through unchanged.
    """
    thinking = ThinkingConfig(type="disabled")
    request = _build_request(thinking=thinking)

    assert request.thinking is not None
    assert request.thinking.type == "disabled"

    dumped = request.model_dump(exclude_none=True)
    assert dumped["thinking"]["type"] == "disabled"


def test_thinking_field_reaches_the_translated_request() -> None:
    """An enabled thinking config on ``AgentRequest`` produces a
    ``MessagesRequest`` carrying the identical config.

    AC1 from SP-AGENT-THINKING-CONTROL.
    """
    from ferova.llm_proxy.api.agent_dispatcher import _translate_request

    thinking = ThinkingConfig(type="enabled", budget_tokens=1024)
    request = _build_request(thinking=thinking)
    translated = _translate_request(request, "test-model")

    assert translated.thinking is not None
    assert translated.thinking.type == "enabled"
    assert translated.thinking.budget_tokens == 1024


def test_absent_thinking_field_translates_to_none() -> None:
    """No field on ``AgentRequest`` → the translated request's
    ``thinking`` is ``None`` (today's behaviour pinned).

    AC2 from SP-AGENT-THINKING-CONTROL.
    """
    from ferova.llm_proxy.api.agent_dispatcher import _translate_request

    request = _build_request()
    translated = _translate_request(request, "test-model")

    assert translated.thinking is None


def test_disabled_thinking_round_trips() -> None:
    """``{"type": "disabled"}`` survives translation intact.

    AC3 from SP-AGENT-THINKING-CONTROL.
    """
    from ferova.llm_proxy.api.agent_dispatcher import _translate_request

    thinking = ThinkingConfig(type="disabled")
    request = _build_request(thinking=thinking)
    translated = _translate_request(request, "test-model")

    assert translated.thinking is not None
    assert translated.thinking.type == "disabled"


# ---------------------------------------------------------------------------
# ProxyGatewayClient.call — thinking kwarg threading (step 3/4)
# ---------------------------------------------------------------------------


class _StubResponse:
    def __init__(self, *, status_code: int, payload: Any | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = ""

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


def _ok_payload() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "capability": "sonnet",
        "stop_reason": "end_turn",
        "model_used": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": "PONG"}],
        "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
        "elapsed_ms": 100,
        "trace": [],
    }


def _client() -> ProxyGatewayClient:
    return ProxyGatewayClient(
        base_url="http://localhost:8082",
        api_key="test-token",
        timeout_s=10.0,
    )


def test_proxy_client_threads_thinking_to_body(monkeypatch) -> None:
    """``ProxyGatewayClient.call(..., thinking=...)`` carries the thinking
    object verbatim into the POST body.

    Step 3/4 of SP-AGENT-THINKING-CONTROL — the client-side kwarg must
    land on the wire so the dispatcher's translator can copy it onto
    the built ``MessagesRequest``.
    """
    stub = _StubClient(_StubResponse(status_code=200, payload=_ok_payload()))
    monkeypatch.setattr(
        "ferova.agent_engine.adapters.httpx.Client",
        lambda **_kw: stub,
    )

    _client().call(
        capability=CapabilityTier.SONNET,
        system="you are X",
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        tools=[],
        max_tokens=100,
        temperature=0.1,
        thinking={"type": "enabled", "budget_tokens": 1024},
    )

    assert len(stub.posted) == 1
    body = stub.posted[0]["json"]
    assert "thinking" in body
    assert body["thinking"]["type"] == "enabled"
    assert body["thinking"]["budget_tokens"] == 1024


def test_proxy_client_omits_thinking_when_unset(monkeypatch) -> None:
    """A ``call`` without the ``thinking`` kwarg omits the field from the body.

    Step 3/4 of SP-AGENT-THINKING-CONTROL — every existing caller keeps
    today's behaviour (no thinking config on the translated request).
    """
    stub = _StubClient(_StubResponse(status_code=200, payload=_ok_payload()))
    monkeypatch.setattr(
        "ferova.agent_engine.adapters.httpx.Client",
        lambda **_kw: stub,
    )

    _client().call(
        capability=CapabilityTier.SONNET,
        system="you are X",
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        tools=[],
        max_tokens=100,
        temperature=0.1,
    )

    assert len(stub.posted) == 1
    body = stub.posted[0]["json"]
    assert "thinking" not in body
