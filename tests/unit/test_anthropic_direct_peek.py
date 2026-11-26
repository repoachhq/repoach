"""Direct Anthropic provider: the failover peek must see its replies as content.

Before the fix, the generic anthropic transport streamed line chunks; the peek
parses one whole SSE event per chunk, so a normal reply was classified
empty_completion and the provider was breaker-tripped on every call.
"""

from __future__ import annotations

import asyncio

from repoach.llm_proxy.api._failover import peek_for_content
from repoach.llm_proxy.providers.base import ProviderConfig
from repoach.llm_proxy.providers.catalog import PROVIDER_DESCRIPTORS
from repoach.llm_proxy.providers.registry import _build_generic_anthropic

# Shape recorded from api.anthropic.com on 2026-11-26 (claude-haiku-5-5).
_STREAM = [
    "event: message_start",
    'data: {"type":"message_start","message":{"model":"claude-haiku-5-5","id":"msg_1","type":"message","role":"assistant","content":[],"stop_reason":null,"usage":{"input_tokens":18,"output_tokens":1}}}',
    "",
    "event: content_block_start",
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}      }',
    "",
    "event: ping",
    'data: {"type": "ping"}',
    "",
    "event: content_block_delta",
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}              }',
    "",
    "event: content_block_stop",
    'data: {"type":"content_block_stop","index":0              }',
    "",
    "event: message_delta",
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":4}}',
    "",
    "event: message_stop",
    'data: {"type":"message_stop"}',
    "",
]


class _FakeResponse:
    async def aiter_lines(self):
        for line in _STREAM:
            yield line


def _provider():
    return _build_generic_anthropic(
        ProviderConfig(api_key="test-key"), PROVIDER_DESCRIPTORS["anthropic"]
    )


def test_direct_anthropic_reply_is_seen_as_content():
    provider = _provider()
    chunks = provider._iter_stream_chunks(_FakeResponse(), state=None, thinking_enabled=False)
    peek = asyncio.run(peek_for_content(chunks))
    assert peek.got_content, "a normal Anthropic reply must not be an empty completion"
    assert peek.stream_done


def test_line_chunks_would_hide_the_reply():
    """Pins why the mode matters: line chunks carry no full event."""
    provider = _provider()
    provider.stream_chunk_mode = "line"
    chunks = provider._iter_stream_chunks(_FakeResponse(), state=None, thinking_enabled=False)
    peek = asyncio.run(peek_for_content(chunks))
    assert not peek.got_content
