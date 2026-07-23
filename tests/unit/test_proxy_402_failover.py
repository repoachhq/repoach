"""Unit tests for SP-PROXY-402-FAILOVER.

The OpenRouter provider override ``_emit_error_events`` used to emit a
terminal ``message_delta`` with ``stop_reason="end_turn"`` and
``output_tokens=1`` on any upstream 4xx (e.g. a 402 "Insufficient
credits"). ``peek_for_content`` then classified the streamed error text
as real model content (``got_content=True reason=real_text``) and the
chain-failover loop served the error string instead of advancing to the
next chain entry (e.g. NIM).

The fix emits ``stop_reason="error"`` with ``output_tokens=0`` so the
peek layer classifies the stream as a failure, exactly like the
OpenAI-compatible transport already does (SP-PROXY-410-AUTO-SKIP).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from repoach.llm_proxy.api._failover import peek_for_content
from repoach.llm_proxy.providers.base import ProviderConfig
from repoach.llm_proxy.providers.open_router.client import OpenRouterProvider

_ERROR_MESSAGE = (
    "Client error '402 Payment Required': "
    "Insufficient credits. Add more using https://openrouter.ai/settings/credits"
)


def _provider() -> OpenRouterProvider:
    return OpenRouterProvider(ProviderConfig(api_key="x"))


def _emit_chunks(*, sent_any_event: bool = False) -> list[str]:
    provider = _provider()
    request = SimpleNamespace(model="claude-opus-4-7")
    return list(
        provider._emit_error_events(
            request=request,
            input_tokens=12,
            error_message=_ERROR_MESSAGE,
            sent_any_event=sent_any_event,
        )
    )


def _message_delta_payload(chunks: list[str]) -> dict:
    for chunk in chunks:
        if "event: message_delta" in chunk:
            data_line = next(line for line in chunk.splitlines() if line.startswith("data: "))
            return json.loads(data_line[len("data: ") :])
    raise AssertionError("no message_delta event emitted")


def test_error_emission_uses_error_stop_reason() -> None:
    """The terminal ``message_delta`` carries ``stop_reason="error"``
    and ``output_tokens=0`` so the failover layer treats it as a failure,
    not as content."""
    payload = _message_delta_payload(_emit_chunks())
    assert payload["delta"]["stop_reason"] == "error"
    assert payload["usage"]["output_tokens"] == 0


def test_error_emission_keeps_anthropic_shape() -> None:
    """A fresh stream (``sent_any_event=False``) still opens with
    ``message_start``, surfaces the error text, and closes with
    ``message_stop`` so single-entry chains degrade gracefully."""
    chunks = _emit_chunks(sent_any_event=False)
    joined = "".join(chunks)
    assert "event: message_start" in joined
    assert "event: message_stop" in joined
    assert "Insufficient credits" in joined


def test_error_emission_omits_message_start_when_already_sent() -> None:
    """When earlier events already streamed, no second ``message_start``
    is emitted (the existing contract is preserved)."""
    chunks = _emit_chunks(sent_any_event=True)
    assert all("event: message_start" not in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_peek_classifies_openrouter_error_as_failure() -> None:
    """End-to-end with the classifier: the chunks the provider emits on a
    402 are seen by ``peek_for_content`` as a failure (``got_content`` is
    False), so ``_stream_with_failover`` advances to the next chain entry."""

    async def _stream() -> AsyncIterator[str]:
        for chunk in _emit_chunks():
            yield chunk

    result = await peek_for_content(_stream())
    assert result.got_content is False
    assert result.stream_done is False
