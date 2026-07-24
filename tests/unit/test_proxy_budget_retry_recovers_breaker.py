"""Unit test for SP-PROXY-ENV-RESIDUE G6 (audit 2026-07-13 residue item).

A successful budget-retry must recover the candidate's breaker exactly
like the normal (non-retry) success path: reset the consecutive-failure
counter via ``get_breaker().recover(...)`` AND log a recovery event.
SP-BREAKER-SLOW-STRIKE already wired the ``recover()`` call into the
budget-retry success branch of ``ClaudeProxyService._stream_with_failover``;
the recovery LOG that mirrors the normal path's ``proxy_chain_failover_recovered``
event was the one piece of residue still missing.

Drives the REAL ``_stream_with_failover`` against a real
:class:`OpenRouterProvider` backed by an ``httpx.MockTransport`` (a
truthful boundary fake — only the HTTP transport is faked, no Repoach
code is monkeypatched): the mock upstream returns a budget-starved empty
completion below a ``max_tokens`` threshold and real content at or above
it, so the SAME candidate is retried with an enlarged budget exactly as
production traffic would exercise it.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest
from loguru import logger as loguru_logger

from repoach.llm_proxy.api.model_router import ResolvedModel
from repoach.llm_proxy.api.models.anthropic import Message, MessagesRequest
from repoach.llm_proxy.api.services import ClaudeProxyService
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.base import ProviderConfig
from repoach.llm_proxy.providers.open_router.client import OpenRouterProvider
from repoach.llm_proxy.routing import get_breaker, reset_breaker
from repoach.llm_proxy.routing.refs import ModelRef

_BUDGET_THRESHOLD = 500

_STARVED_EMPTY = (
    "event: message_start\n"
    'data: {"type":"message_start","message":{"id":"msg_x","type":"message",'
    '"role":"assistant","content":[],"model":"fake","stop_reason":null,'
    '"stop_sequence":null,"usage":{"input_tokens":10,"output_tokens":0}}}\n\n'
    "event: message_delta\n"
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},'
    '"usage":{"input_tokens":10,"output_tokens":0}}\n\n'
    "event: message_stop\n"
    'data: {"type":"message_stop"}\n\n'
)

_REAL_CONTENT = (
    "event: message_start\n"
    'data: {"type":"message_start","message":{"id":"msg_x","type":"message",'
    '"role":"assistant","content":[],"model":"fake","stop_reason":null,'
    '"stop_sequence":null,"usage":{"input_tokens":10,"output_tokens":0}}}\n\n'
    "event: content_block_start\n"
    'data: {"type":"content_block_start","index":0,'
    '"content_block":{"type":"text","text":""}}\n\n'
    "event: content_block_delta\n"
    'data: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"text_delta","text":"HELLO"}}\n\n'
    "event: content_block_stop\n"
    'data: {"type":"content_block_stop","index":0}\n\n'
    "event: message_delta\n"
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},'
    '"usage":{"input_tokens":10,"output_tokens":12}}\n\n'
    "event: message_stop\n"
    'data: {"type":"message_stop"}\n\n'
)


@pytest.fixture(autouse=True)
def _hermetic_breaker() -> None:
    reset_breaker()


def _budget_sensitive_transport() -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        content = (
            _REAL_CONTENT if body.get("max_tokens", 0) >= _BUDGET_THRESHOLD else _STARVED_EMPTY
        )
        return httpx.Response(
            200,
            content=content,
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    return httpx.MockTransport(handler)


def _open_router_provider() -> OpenRouterProvider:
    provider = OpenRouterProvider(ProviderConfig(api_key="test-key"))
    provider._client = httpx.AsyncClient(
        base_url="https://mock.example",
        transport=_budget_sensitive_transport(),
    )
    return provider


def _candidate() -> ResolvedModel:
    return ResolvedModel(
        original_model="claude-sonnet-4",
        provider_id="open_router",
        provider_model="anthropic/claude-sonnet-4",
        provider_model_ref="open_router/anthropic/claude-sonnet-4",
    )


def _request() -> MessagesRequest:
    return MessagesRequest(
        model="claude-sonnet-4",
        max_tokens=128,
        messages=[Message(role="user", content="ping")],
    )


def _service(monkeypatch: pytest.MonkeyPatch, provider: OpenRouterProvider) -> ClaudeProxyService:
    monkeypatch.setenv("REPOACH_BREAKER_ENABLED", "true")
    monkeypatch.setenv("REPOACH_PROXY_BUDGET_RETRY_ENABLED", "true")
    monkeypatch.setenv("REPOACH_PROXY_BUDGET_RETRY_FACTOR", "8")
    monkeypatch.setenv("REPOACH_PROXY_BUDGET_RETRY_FLOOR", "512")
    monkeypatch.setenv("REPOACH_PROXY_BUDGET_RETRY_CAP", "8192")
    settings = Settings(_env_file=None)
    return ClaudeProxyService(
        settings=settings,
        provider_getter=lambda _provider_id: provider,
        token_counter=lambda *_args, **_kwargs: 0,
    )


async def _drain(service: ClaudeProxyService, chain: list[ResolvedModel]) -> list[str]:
    chunks: list[str] = []
    async for chunk in service._stream_with_failover(_request(), chain, input_tokens=5):
        chunks.append(chunk)
    return chunks


def _capture_loguru() -> tuple[list[Any], int]:
    records: list[Any] = []
    sink_id = loguru_logger.add(
        lambda msg: records.append(msg.record),
        format="{message}",
        level="DEBUG",
    )
    return records, sink_id


def test_budget_retry_success_resets_breaker_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    """A budget-retry that succeeds resets the candidate's consecutive-failure
    counter to 0 and emits a recovery log event, matching the normal
    (non-retry) success path.
    """
    provider = _open_router_provider()
    service = _service(monkeypatch, provider)
    ref = ModelRef.parse("open_router/anthropic/claude-sonnet-4")
    chain = [_candidate()]

    breaker = get_breaker()
    breaker._consecutive_failures[ref] = 2

    records, sink_id = _capture_loguru()
    try:
        chunks = asyncio.run(_drain(service, chain))
    finally:
        loguru_logger.remove(sink_id)

    assert "HELLO" in "".join(chunks)
    assert breaker._consecutive_failures.get(ref, 0) == 0
    assert not breaker.is_down(ref, now=time.monotonic())

    recovery_logs = [r for r in records if r["message"] == "proxy_chain_failover_recovered"]
    assert len(recovery_logs) >= 1, "budget-retry success must log a recovery event"
