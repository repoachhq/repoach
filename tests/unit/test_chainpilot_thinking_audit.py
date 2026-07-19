"""Characterization of the thinking path (SP-CHAINPILOT-THINKING-AUDIT, 0a).

These tests PIN today's behaviour so Phase 0b/0c refactor against a green
safety net. They assert two things the audit
(`docs/chain_autopilot_thinking_audit.md`) relies on:

1. `peek_for_content` already separates a budget-starved stream (retryable)
   from a dead one — the spine that lets a thinking model be retried rather
   than blindly failed over.
2. The reasoning-budget gap: NIM bounds the reasoning budget so visible
   output keeps headroom; the generic OpenAI path bounds nothing. This is
   exactly the gap Phase 0b closes.

No production behaviour is exercised beyond reading the existing code paths.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from repoach.llm_proxy.api._failover import peek_for_content
from repoach.llm_proxy.api.models.anthropic import Message, MessagesRequest
from repoach.llm_proxy.config.nim import NimSettings
from repoach.llm_proxy.core.anthropic import build_base_request_body
from repoach.llm_proxy.providers.nvidia_nim.request import build_request_body


def _sse(event: str, data: dict | None) -> str:
    payload = "[DONE]" if data is None else json.dumps(data)
    return f"event: {event}\ndata: {payload}\n\n"


async def _stream(chunks: list[str]) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


def _peek(chunks: list[str]):
    return asyncio.run(peek_for_content(_stream(chunks)))


def _message_delta(stop_reason: str, output_tokens: int) -> str:
    return _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason},
            "usage": {"output_tokens": output_tokens},
        },
    )


def _text_delta(text: str) -> str:
    return _sse(
        "content_block_delta",
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
    )


_STOP = _sse("message_stop", {"type": "message_stop"})


class TestPeekSeparatesStarvedFromDead:
    def test_zero_output_tokens_is_budget_starved(self) -> None:
        result = _peek([_message_delta("end_turn", 0), _STOP])
        assert result.got_content is False
        assert result.looks_budget_starved is True

    def test_whitespace_only_is_budget_starved(self) -> None:
        result = _peek([_text_delta("   "), _message_delta("end_turn", 1), _STOP])
        assert result.got_content is False
        assert result.looks_budget_starved is True

    def test_error_stop_reason_is_not_starved(self) -> None:
        result = _peek([_message_delta("error", 0), _STOP])
        assert result.got_content is False
        assert result.looks_budget_starved is False

    def test_real_text_is_content_not_starved(self) -> None:
        result = _peek([_text_delta("hello"), _message_delta("end_turn", 5), _STOP])
        assert result.got_content is True
        assert result.looks_budget_starved is False


def _thinking_request() -> MessagesRequest:
    return MessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[Message(role="user", content="ping")],
    )


class TestReasoningBudgetGap:
    def test_nim_bounds_the_reasoning_budget(self) -> None:
        body = build_request_body(_thinking_request(), NimSettings(), thinking_enabled=True)
        kwargs = body.get("extra_body", {}).get("chat_template_kwargs", {})
        budget = kwargs.get("reasoning_budget")
        assert isinstance(budget, int)
        assert 0 < budget <= 2048

    def test_generic_openai_path_bounds_nothing(self) -> None:
        body = build_base_request_body(_thinking_request(), include_thinking=True)
        serialized = json.dumps(body)
        assert "reasoning_budget" not in serialized
        assert "chat_template_kwargs" not in serialized
