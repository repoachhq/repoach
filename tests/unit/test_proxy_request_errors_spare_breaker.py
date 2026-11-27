"""A provider's request rejection is a request bug, not a provider outage.

On 2026-11-27 (CI run 45986316427) every reviewer call to the direct
``anthropic`` provider sent ``temperature``, which claude-opus-5-5,
claude-sonnet-5-5 and claude-haiku-5-5 reject with HTTP 400
``invalid_request_error`` ("`temperature` is deprecated for this model.").
The native Anthropic transport degraded the 400 to an in-band SSE
``error`` event, the failover peek ignored that event and classified the
stream ``output_tokens_zero``, the dispatcher recorded
``empty_completion`` and struck the breaker, and the third strike
quarantined Anthropic for the whole job.

These tests drive the real dispatch path: the real transports built by
the proxy, the real ``ClaudeProxyService._stream_with_failover``, the
real peek and the real process-level breaker. Only the HTTP boundary is
faked, through ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterator

import httpx
import pytest
from loguru import logger

from repoach.llm_proxy.api.model_router import ResolvedModel
from repoach.llm_proxy.api.models.anthropic import Message, MessagesRequest
from repoach.llm_proxy.api.services import ClaudeProxyService, _classify_failover_reason
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.base import BaseProvider, ProviderConfig
from repoach.llm_proxy.providers.catalog import PROVIDER_DESCRIPTORS
from repoach.llm_proxy.providers.exceptions import UpstreamStatusError
from repoach.llm_proxy.providers.open_router.client import OpenRouterProvider
from repoach.llm_proxy.providers.registry import _build_generic_anthropic
from repoach.llm_proxy.routing import get_breaker
from repoach.llm_proxy.routing.refs import ModelRef

REJECTING_MODEL = "claude-opus-5-5"
ACCEPTING_MODEL = "claude-opus-4-1"
TEMPERATURE_REJECTION = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": "`temperature` is deprecated for this model.",
    },
    "request_id": "req_011CVtemperature",
}
QUARANTINE_THRESHOLD = 3

_SUCCESS_EVENTS: list[tuple[str, dict]] = [
    (
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": ACCEPTING_MODEL,
                "stop_reason": None,
                "usage": {"input_tokens": 5, "output_tokens": 1},
            },
        },
    ),
    (
        "content_block_start",
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    ),
    (
        "content_block_delta",
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
    ),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    (
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 2},
        },
    ),
    ("message_stop", {"type": "message_stop"}),
]
_SUCCESS_STREAM = "".join(
    f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in _SUCCESS_EVENTS
)

ProviderBuilder = Callable[[int], BaseProvider]


@pytest.fixture
def records() -> Iterator[list[dict]]:
    """Capture every loguru record emitted while a test runs.

    Yields:
        The list the sink appends each record to, in emission order.
    """
    captured: list[dict] = []
    sink_id = logger.add(lambda message: captured.append(message.record), level="DEBUG")
    yield captured
    logger.remove(sink_id)


def _status_body(status_code: int) -> dict:
    """Build the error body an upstream returns with ``status_code``.

    Args:
        status_code: The HTTP status the fake upstream answers with.

    Returns:
        The recorded Anthropic temperature rejection for a 400, a generic
        Anthropic error body for any other status.
    """
    if status_code == 400:
        return TEMPERATURE_REJECTION
    return {"type": "error", "error": {"type": "api_error", "message": f"HTTP {status_code}"}}


def _anthropic_provider(status_code: int) -> BaseProvider:
    """Build the direct Anthropic transport the way the proxy registry does.

    The fake upstream answers ``status_code`` for ``REJECTING_MODEL`` and a
    recorded successful stream for any other model.

    Args:
        status_code: The HTTP status returned for ``REJECTING_MODEL``.

    Returns:
        The registry-built transport whose HTTP client talks to the fake.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        """Answer one Messages call from the model named in its body.

        Args:
            request: The outgoing Messages request.

        Returns:
            The rejection for ``REJECTING_MODEL``, a stream otherwise.
        """
        if json.loads(request.content)["model"] == REJECTING_MODEL:
            return httpx.Response(status_code, json=_status_body(status_code), request=request)
        return httpx.Response(
            200,
            content=_SUCCESS_STREAM.encode(),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    provider = _build_generic_anthropic(
        ProviderConfig(api_key="test-key"), PROVIDER_DESCRIPTORS["anthropic"]
    )
    provider._client = httpx.AsyncClient(
        base_url="https://api.anthropic.test/v1", transport=httpx.MockTransport(handler)
    )
    return provider


def _open_router_provider(status_code: int) -> BaseProvider:
    """Build the OpenRouter transport with a fake upstream answering ``status_code``.

    Args:
        status_code: The HTTP status every call receives.

    Returns:
        The OpenRouter transport whose HTTP client talks to the fake.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        """Answer every call with ``status_code`` and a matching error body.

        Args:
            request: The outgoing Messages request.

        Returns:
            The fake upstream response.
        """
        return httpx.Response(status_code, json=_status_body(status_code), request=request)

    provider = OpenRouterProvider(ProviderConfig(api_key="test-key"))
    provider._client = httpx.AsyncClient(
        base_url="https://openrouter.test/api/v1", transport=httpx.MockTransport(handler)
    )
    return provider


def _provider_id(provider: BaseProvider) -> str:
    """Name the chain provider id a transport serves.

    Args:
        provider: A transport built by one of the builders above.

    Returns:
        ``"open_router"`` for the OpenRouter transport, ``"anthropic"`` otherwise.
    """
    if isinstance(provider, OpenRouterProvider):
        return "open_router"
    return "anthropic"


def _candidate(ref: str) -> ResolvedModel:
    """Resolve a ``provider/model`` ref into a chain candidate.

    Args:
        ref: The ``provider/model`` reference as written in ``chains.env``.

    Returns:
        The chain candidate for ``ref``.
    """
    provider_id, provider_model = ref.split("/", 1)
    return ResolvedModel(
        original_model="claude-opus",
        provider_id=provider_id,
        provider_model=provider_model,
        provider_model_ref=ref,
    )


def _service(monkeypatch: pytest.MonkeyPatch, provider: BaseProvider) -> ClaudeProxyService:
    """Build the proxy service with the breaker on and every candidate served by ``provider``.

    Args:
        monkeypatch: Sets the breaker environment for this test only.
        provider: The transport every candidate dispatches to.

    Returns:
        The proxy service under test.
    """
    monkeypatch.setenv("REPOACH_BREAKER_ENABLED", "true")
    monkeypatch.setenv("REPOACH_BREAKER_TTL_S", "120")
    monkeypatch.setenv("REPOACH_BREAKER_TTL_QUARANTINE_S", "21600")
    monkeypatch.setenv("REPOACH_BREAKER_QUARANTINE_THRESHOLD", str(QUARANTINE_THRESHOLD))
    return ClaudeProxyService(
        settings=Settings(_env_file=None),
        provider_getter=lambda _provider_id: provider,
        token_counter=lambda *_args, **_kwargs: 0,
    )


def _dispatch(service: ClaudeProxyService, chain: list[ResolvedModel]) -> str:
    """Run one chain-walked dispatch carrying ``temperature``, as a reviewer call does.

    Args:
        service: The proxy service under test.
        chain: The resolved chain to walk.

    Returns:
        Every SSE chunk the dispatcher yielded, joined.
    """
    request = MessagesRequest(
        model="claude-opus",
        max_tokens=64,
        temperature=0.0,
        messages=[Message(role="user", content="review this diff")],
    )

    async def drain() -> str:
        """Drain the dispatcher's stream.

        Returns:
            The joined SSE chunks.
        """
        chunks = [
            chunk async for chunk in service._stream_with_failover(request, chain, input_tokens=5)
        ]
        return "".join(chunks)

    return asyncio.run(drain())


def _failover_events(records: list[dict]) -> list[dict]:
    """Return the structured fields of every ``proxy_chain_failover_fired`` record.

    Args:
        records: The captured loguru records.

    Returns:
        The ``extra`` payload of each failover event, in emission order.
    """
    return [r["extra"] for r in records if r["message"] == "proxy_chain_failover_fired"]


@pytest.mark.parametrize(
    ("build_provider", "peek_reason"),
    [(_anthropic_provider, "upstream_error"), (_open_router_provider, "stop_reason_error")],
)
def test_invalid_request_400_fails_over_without_breaker_strike(
    monkeypatch: pytest.MonkeyPatch,
    records: list[dict],
    build_provider: ProviderBuilder,
    peek_reason: str,
) -> None:
    """A 400 ``invalid_request_error`` fails over but never strikes or quarantines.

    The dispatch is repeated past the quarantine threshold: before the fix
    every rejection struck the breaker, and the third one quarantined the
    candidate. The failover event names the real cause, status and error
    type, instead of ``empty_completion``; the peek of the native Anthropic
    error event names it instead of ``output_tokens_zero``. OpenRouter
    degrades the same 400 to a terminal ``message_delta``, which the peek
    already read as ``stop_reason_error``.
    """
    provider = build_provider(400)
    service = _service(monkeypatch, provider)
    rejecting = _candidate(f"{_provider_id(provider)}/{REJECTING_MODEL}")
    rejecting_ref = ModelRef.parse(rejecting.provider_model_ref)

    for _ in range(QUARANTINE_THRESHOLD + 1):
        _dispatch(service, [rejecting])

    assert not get_breaker().is_down(rejecting_ref, time.monotonic()), (
        "a request rejection must not trip the breaker"
    )
    assert get_breaker()._consecutive_failures.get(rejecting_ref) is None, (
        "a request rejection must not count as a breaker strike"
    )
    assert [r for r in records if r["message"] == "breaker_quarantined"] == []
    events = _failover_events(records)
    assert len(events) == QUARANTINE_THRESHOLD + 1
    for event in events:
        assert event["primary_reason"] == "invalid_request"
        assert event["upstream_status_code"] == 400
        assert event["upstream_error_type"] == "invalid_request_error"
        assert event["breaker_strike"] is False
    peeks = [r["message"] for r in records if r["message"].startswith("PEEK_DECISION")]
    assert len(peeks) == QUARANTINE_THRESHOLD + 1
    for peek in peeks:
        assert f"reason={peek_reason} " in peek
        assert "upstream_status=400" in peek
        assert "upstream_error_type=invalid_request_error" in peek


def test_invalid_request_400_still_fails_over_to_next_candidate(
    monkeypatch: pytest.MonkeyPatch, records: list[dict]
) -> None:
    """The next chain candidate serves the request the first one rejected.

    A model that rejects ``temperature`` says nothing about the next model
    of the chain, which may accept the same request; failing over keeps the
    reviewer served while the request bug is fixed at its source.
    """
    provider = _anthropic_provider(400)
    service = _service(monkeypatch, provider)
    rejecting = _candidate(f"anthropic/{REJECTING_MODEL}")
    accepting = _candidate(f"anthropic/{ACCEPTING_MODEL}")

    body = _dispatch(service, [rejecting, accepting])

    assert '"text": "ok"' in body
    assert not get_breaker().is_down(ModelRef.parse(rejecting.provider_model_ref), time.monotonic())
    served = [r["extra"] for r in records if r["message"] == "proxy_chain_failover_recovered"]
    assert [event["served_by"] for event in served] == [accepting.provider_model_ref]


@pytest.mark.parametrize(
    ("status_code", "reason"),
    [
        (401, "auth_failed_propagated"),
        (402, "provider_402_propagated"),
        (429, "rate_limited"),
        (503, "provider_5xx"),
    ],
)
@pytest.mark.parametrize("build_provider", [_anthropic_provider, _open_router_provider])
def test_outage_statuses_still_strike(
    monkeypatch: pytest.MonkeyPatch,
    records: list[dict],
    build_provider: ProviderBuilder,
    status_code: int,
    reason: str,
) -> None:
    """Auth, billing, rate-limit and server failures keep striking the breaker.

    They say something about the provider, not about the request, so the
    candidate is tripped once, with a reason naming the real status.
    """
    provider = build_provider(status_code)
    service = _service(monkeypatch, provider)
    candidate = _candidate(f"{_provider_id(provider)}/{REJECTING_MODEL}")
    ref = ModelRef.parse(candidate.provider_model_ref)

    _dispatch(service, [candidate])

    assert get_breaker().is_down(ref, time.monotonic())
    assert get_breaker()._consecutive_failures[ref] == 1
    assert get_breaker()._down_reason[ref] == reason
    assert [event["breaker_strike"] for event in _failover_events(records)] == [True]


def test_request_rejection_statuses_classify_as_invalid_request() -> None:
    """A recovered 400 or 422 is a request rejection; 404 stays a dead-hop quarantine."""
    assert _classify_failover_reason(UpstreamStatusError(400)) == "invalid_request"
    assert _classify_failover_reason(UpstreamStatusError(422)) == "invalid_request"
    assert _classify_failover_reason(UpstreamStatusError(404)) == "provider_404"
    assert _classify_failover_reason(UpstreamStatusError(402)) == "provider_402"
