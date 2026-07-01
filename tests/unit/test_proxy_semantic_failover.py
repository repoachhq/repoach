"""Unit tests for SP-PROXY-SEMANTIC-FAILOVER.

Covers the four layers the semantic-failover plumbing touches :

1. :meth:`ModelRouter.resolve_chain` honours the ``skip_models``
   parameter and degrades gracefully when every candidate is skipped.
2. :class:`ProxyGatewayClient.call` forwards ``skip_models`` to the
   proxy as part of the :class:`AgentRequest` body.
3. :meth:`AgentLoop.run_oneshot` retries with a growing skip-list
   when ``accept_response`` rejects a candidate and raises
   ``RuntimeError`` only after the full chain is exhausted.
4. :meth:`Reviewer._response_is_parsable` returns ``False`` on a
   parse-failed shape (whitespace-only / malformed) and ``True`` on
   a clean JSON verdict.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from ferova.agent_engine.agent_loop import AgentLoop
from ferova.llm_proxy.api.model_router import ModelRouter
from ferova.llm_proxy.config.settings import Settings
from ferova.review.reviewer import Architect


@pytest.fixture(autouse=True)
def _stub_proxy_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep AgentLoop constructible without the operator ``.env``.

    SP-PROXY-SECURE-DEFAULTS removed the implicit ``freecc`` token
    fallback, so a tokenless environment (CI) now refuses to build the
    loop — these tests target semantic failover, not auth.
    """
    monkeypatch.setattr(
        "ferova.agent_engine.agent_loop.get_settings",
        lambda: SimpleNamespace(
            llm_proxy_base_url="http://localhost:8082",
            llm_proxy_auth_token=SecretStr("test-token"),
        ),
    )


# ---------------------------------------------------------------------------
# Layer 1 — ModelRouter.resolve_chain(skip_models=...)
# ---------------------------------------------------------------------------


@pytest.fixture()
def three_link_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("MODEL", "nvidia_nim/z-ai/glm4.7")
    monkeypatch.setenv(
        "MODEL_SONNET",
        "nvidia_nim/moonshotai/kimi-k2.6,kimi/kimi-k2.6,nvidia_nim/deepseek-ai/deepseek-v4-flash",
    )
    return Settings()


def test_resolve_chain_filters_skip_models(three_link_settings: Settings) -> None:
    router = ModelRouter(three_link_settings)
    chain = router.resolve_chain(
        "claude-sonnet-4-6",
        skip_models=frozenset({"nvidia_nim/moonshotai/kimi-k2.6"}),
    )
    refs = [entry.provider_model_ref for entry in chain]
    assert "nvidia_nim/moonshotai/kimi-k2.6" not in refs
    assert refs == ["kimi/kimi-k2.6", "nvidia_nim/deepseek-ai/deepseek-v4-flash"]


def test_resolve_chain_filters_multiple_skip_models(three_link_settings: Settings) -> None:
    router = ModelRouter(three_link_settings)
    chain = router.resolve_chain(
        "claude-sonnet-4-6",
        skip_models=frozenset(
            {
                "nvidia_nim/moonshotai/kimi-k2.6",
                "kimi/kimi-k2.6",
            }
        ),
    )
    refs = [entry.provider_model_ref for entry in chain]
    assert refs == ["nvidia_nim/deepseek-ai/deepseek-v4-flash"]


def test_resolve_chain_falls_back_to_head_when_all_skipped(
    three_link_settings: Settings,
) -> None:
    """If every candidate would be skipped, fall back to the head
    candidate so the caller can surface a definitive failure rather
    than looping on an empty chain."""
    router = ModelRouter(three_link_settings)
    chain = router.resolve_chain(
        "claude-sonnet-4-6",
        skip_models=frozenset(
            {
                "nvidia_nim/moonshotai/kimi-k2.6",
                "kimi/kimi-k2.6",
                "nvidia_nim/deepseek-ai/deepseek-v4-flash",
            }
        ),
    )
    refs = [entry.provider_model_ref for entry in chain]
    assert refs == ["nvidia_nim/moonshotai/kimi-k2.6"]


def test_resolve_chain_empty_skip_set_preserves_chain(
    three_link_settings: Settings,
) -> None:
    router = ModelRouter(three_link_settings)
    chain = router.resolve_chain(
        "claude-sonnet-4-6",
        skip_models=frozenset(),
    )
    refs = [entry.provider_model_ref for entry in chain]
    assert refs == [
        "nvidia_nim/moonshotai/kimi-k2.6",
        "kimi/kimi-k2.6",
        "nvidia_nim/deepseek-ai/deepseek-v4-flash",
    ]


# ---------------------------------------------------------------------------
# Layer 2 — ProxyGatewayClient forwards skip_models in AgentRequest
# ---------------------------------------------------------------------------


def _fake_response_payload(text: str = '{"verdict": "APPROVE"}') -> dict[str, Any]:
    return {
        "schema_version": "1",
        "capability": "sonnet",
        "stop_reason": "end_turn",
        "model_used": "nvidia_nim/test",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        "elapsed_ms": 42,
        "trace": [],
    }


def _install_fake_httpx(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, Any],
    *,
    response_text: str = '{"verdict": "APPROVE"}',
) -> None:
    class _FakeResp:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return _fake_response_payload(response_text)

        @property
        def text(self) -> str:
            return ""

    class _FakeHttp:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        def __enter__(self) -> _FakeHttp:
            return self

        def __exit__(self, *_a: Any) -> None:
            pass

        def post(self, url: str, *, json: Any, headers: Any) -> _FakeResp:
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResp()

    monkeypatch.setattr("ferova.agent_engine.adapters.httpx.Client", _FakeHttp)


def test_proxy_gateway_forwards_skip_models_in_request_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ferova.agent_engine.adapters import ProxyGatewayClient
    from ferova.llm.capability import CapabilityTier
    from ferova.llm_proxy.api.models.agent_v1 import AgentResponse, Message, TextBlock

    captured: dict[str, Any] = {}
    _install_fake_httpx(monkeypatch, captured)

    client = ProxyGatewayClient(base_url="http://x", api_key="k", timeout_s=1.0)
    response = client.call(
        capability=CapabilityTier.SONNET,
        system=None,
        messages=[Message(role="user", content=[TextBlock(text="ping")])],
        tools=[],
        max_tokens=64,
        temperature=0.0,
        skip_models=frozenset({"nvidia_nim/kimi", "kimi/kimi"}),
    )
    assert isinstance(response, AgentResponse)
    assert captured["json"]["skip_models"] == ["kimi/kimi", "nvidia_nim/kimi"]


def test_proxy_gateway_omits_skip_models_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty skip-list still serialises to ``skip_models: []`` —
    this is the conservative default that the proxy treats as
    no-filter (per ModelRouter test above)."""
    from ferova.agent_engine.adapters import ProxyGatewayClient
    from ferova.llm.capability import CapabilityTier
    from ferova.llm_proxy.api.models.agent_v1 import Message, TextBlock

    captured: dict[str, Any] = {}
    _install_fake_httpx(monkeypatch, captured)

    client = ProxyGatewayClient(base_url="http://x", api_key="k", timeout_s=1.0)
    client.call(
        capability=CapabilityTier.SONNET,
        system=None,
        messages=[Message(role="user", content=[TextBlock(text="ping")])],
        tools=[],
        max_tokens=64,
        temperature=0.0,
    )
    assert captured["json"]["skip_models"] == []


# ---------------------------------------------------------------------------
# Layer 3 — AgentLoop.run_oneshot retries with growing skip-list
# ---------------------------------------------------------------------------


def _make_loop(monkeypatch: pytest.MonkeyPatch, chain: tuple[str, ...]) -> AgentLoop:
    loop = AgentLoop(model_chain=chain, max_tokens=128, temperature=0.0)
    return loop


def _agent_response(text: str, model_used: str) -> Any:
    """Build a minimal AgentResponse shape with one text block."""
    response = MagicMock()
    block = MagicMock()
    block.text = text
    response.content = [block]
    response.model_used = model_used
    response.usage = MagicMock()
    response.usage.total_tokens = 10
    return response


def test_run_oneshot_no_callback_passes_first_response_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _make_loop(monkeypatch, ("nvidia_nim/a", "kimi/b"))
    fake_client = MagicMock()
    fake_client.call.return_value = _agent_response('{"verdict": "APPROVE"}', "nvidia_nim/a")
    monkeypatch.setattr(loop, "_client", fake_client)

    result = loop.run_oneshot("ping", json_response=True)

    assert fake_client.call.call_count == 1
    assert result.model_used == "nvidia_nim/a"


def test_run_oneshot_accept_response_true_returns_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _make_loop(monkeypatch, ("nvidia_nim/a", "kimi/b"))
    fake_client = MagicMock()
    fake_client.call.return_value = _agent_response('{"verdict": "APPROVE"}', "nvidia_nim/a")
    monkeypatch.setattr(loop, "_client", fake_client)

    result = loop.run_oneshot(
        "ping",
        json_response=True,
        accept_response=lambda _r: True,
    )

    assert fake_client.call.call_count == 1
    assert result.model_used == "nvidia_nim/a"


def test_run_oneshot_rejects_first_walks_to_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = _make_loop(monkeypatch, ("nvidia_nim/a", "kimi/b"))
    fake_client = MagicMock()
    fake_client.call.side_effect = [
        _agent_response(" ", "nvidia_nim/a"),
        _agent_response('{"verdict": "APPROVE"}', "kimi/b"),
    ]
    monkeypatch.setattr(loop, "_client", fake_client)

    accept_calls: list[str] = []

    def accept(response: Any) -> bool:
        text = response.content[0].text
        accept_calls.append(text)
        return text.strip() != ""

    result = loop.run_oneshot("ping", json_response=True, accept_response=accept)

    assert fake_client.call.call_count == 2
    assert result.model_used == "kimi/b"
    assert accept_calls == [" ", '{"verdict": "APPROVE"}']
    second_call_kwargs = fake_client.call.call_args_list[1].kwargs
    assert second_call_kwargs["skip_models"] == frozenset({"nvidia_nim/a"})


def test_run_oneshot_raises_after_full_chain_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the proxy has exhausted its real chain it falls back to
    the head candidate (per ModelRouter behaviour) ; the loop
    detects the duplicate ``model_used`` against its skip-list and
    raises rather than spinning forever."""
    loop = _make_loop(monkeypatch, ("nvidia_nim/a", "kimi/b"))
    fake_client = MagicMock()
    fake_client.call.side_effect = [
        _agent_response(" ", "nvidia_nim/a"),
        _agent_response("", "kimi/b"),
        _agent_response(" ", "nvidia_nim/a"),
    ]
    monkeypatch.setattr(loop, "_client", fake_client)

    with pytest.raises(RuntimeError) as excinfo:
        loop.run_oneshot(
            "ping",
            json_response=True,
            accept_response=lambda r: r.content[0].text.strip() != "",
        )

    assert "exhausting" in str(excinfo.value)
    assert fake_client.call.call_count == 3


def test_run_oneshot_breaks_when_model_used_already_in_skip_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the proxy returns the same ``model_used`` after we already
    skipped it (defensive — proxy fallback to head when every
    candidate was filtered out), the loop must break to avoid an
    infinite retry."""
    loop = _make_loop(monkeypatch, ("nvidia_nim/a", "kimi/b", "groq/c"))
    fake_client = MagicMock()
    fake_client.call.side_effect = [
        _agent_response(" ", "nvidia_nim/a"),
        _agent_response("", "nvidia_nim/a"),
    ]
    monkeypatch.setattr(loop, "_client", fake_client)

    with pytest.raises(RuntimeError):
        loop.run_oneshot(
            "ping",
            json_response=True,
            accept_response=lambda r: r.content[0].text.strip() != "",
        )

    assert fake_client.call.call_count == 2


def test_run_oneshot_breaks_when_model_used_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive — if the proxy returns ``model_used=None`` (no
    candidate answered, malformed upstream response), the loop must
    raise immediately on the first reject rather than retrying with
    an empty skip-list addition.  Same break also catches the
    ``model_used=""`` shape some providers emit on transport flake.
    """
    loop = _make_loop(monkeypatch, ("nvidia_nim/a", "kimi/b"))
    fake_client = MagicMock()
    fake_client.call.return_value = _agent_response(" ", None)
    monkeypatch.setattr(loop, "_client", fake_client)

    with pytest.raises(RuntimeError):
        loop.run_oneshot(
            "ping",
            json_response=True,
            accept_response=lambda r: r.content[0].text.strip() != "",
        )

    assert fake_client.call.call_count == 1


def test_run_oneshot_breaks_when_model_used_is_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same defensive break for ``model_used=""`` — some providers
    serialise a missing model field as the empty string rather than
    ``None``.  Both cases must trigger the immediate-raise path."""
    loop = _make_loop(monkeypatch, ("nvidia_nim/a", "kimi/b"))
    fake_client = MagicMock()
    fake_client.call.return_value = _agent_response(" ", "")
    monkeypatch.setattr(loop, "_client", fake_client)

    with pytest.raises(RuntimeError):
        loop.run_oneshot(
            "ping",
            json_response=True,
            accept_response=lambda r: r.content[0].text.strip() != "",
        )

    assert fake_client.call.call_count == 1


# ---------------------------------------------------------------------------
# Layer 4 — Reviewer._response_is_parsable predicate
# ---------------------------------------------------------------------------


def test_response_is_parsable_accepts_clean_json() -> None:
    reviewer = Architect(loop=MagicMock())
    response = MagicMock()
    block = MagicMock()
    block.text = '{"verdict": "APPROVE", "summary": "looks good", "comments": []}'
    response.content = [block]
    assert reviewer._response_is_parsable(response) is True


def test_response_is_parsable_rejects_whitespace_only() -> None:
    reviewer = Architect(loop=MagicMock())
    response = MagicMock()
    block = MagicMock()
    block.text = " "
    response.content = [block]
    assert reviewer._response_is_parsable(response) is False


def test_response_is_parsable_rejects_truncated_json() -> None:
    reviewer = Architect(loop=MagicMock())
    response = MagicMock()
    block = MagicMock()
    block.text = '{"verdict": "APPROVE", "summary'
    response.content = [block]
    assert reviewer._response_is_parsable(response) is False


def test_response_is_parsable_rejects_transport_error_prose() -> None:
    reviewer = Architect(loop=MagicMock())
    response = MagicMock()
    block = MagicMock()
    block.text = "Connection error. (request_id=req_abc123)"
    response.content = [block]
    assert reviewer._response_is_parsable(response) is False
