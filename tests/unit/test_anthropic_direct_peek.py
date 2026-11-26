"""Direct Anthropic provider: the failover peek must see its replies as content.

Before the fix, the generic anthropic transport streamed line chunks; the peek
parses one whole SSE event per chunk, so a normal reply was classified
empty_completion and the provider was breaker-tripped on every call.

``_STREAM`` is the SSE shape recorded from api.anthropic.com on 2026-11-26
(claude-haiku-5-5).
"""

from __future__ import annotations

import asyncio

from repoach.llm_proxy.api._failover import peek_for_content
from repoach.llm_proxy.providers.anthropic_messages import AnthropicMessagesTransport
from repoach.llm_proxy.providers.base import ProviderConfig
from repoach.llm_proxy.providers.catalog import PROVIDER_DESCRIPTORS
from repoach.llm_proxy.providers.registry import _build_generic_anthropic

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
    """HTTP response stand-in that replays the recorded Anthropic stream."""

    async def aiter_lines(self):
        """Yield the recorded SSE lines one by one, as httpx does.

        Yields:
            Each line of ``_STREAM``, blank separators included.
        """
        for line in _STREAM:
            yield line


def _provider():
    """Build the direct Anthropic provider through the real registry builder.

    Returns:
        The transport as the proxy builds it for the ``anthropic`` descriptor.
    """
    return _build_generic_anthropic(
        ProviderConfig(api_key="test-key"), PROVIDER_DESCRIPTORS["anthropic"]
    )


def test_direct_anthropic_reply_is_seen_as_content():
    """A normal direct Anthropic reply reaches the peek as content.

    The registry builds the transport in event mode, so the peek sees the
    text delta and the end of stream instead of an empty completion.
    """
    provider = _provider()
    chunks = provider._iter_stream_chunks(_FakeResponse(), state=None, thinking_enabled=False)
    peek = asyncio.run(peek_for_content(chunks))
    assert peek.got_content, "a normal Anthropic reply must not be an empty completion"
    assert peek.stream_done


def test_event_mode_is_set_at_construction():
    """The chunk mode is a constructor argument, not a later assignment.

    The registry builder and any other construction path get event mode
    from ``__init__``: the value lands on the instance while the class
    default stays ``"line"``, and omitting the argument keeps that default.
    """
    descriptor = PROVIDER_DESCRIPTORS["anthropic"]
    built = AnthropicMessagesTransport(
        ProviderConfig(api_key="test-key"),
        provider_name=descriptor.provider_id,
        default_base_url=descriptor.default_base_url or "",
        stream_chunk_mode="event",
    )
    assert built.__dict__["stream_chunk_mode"] == "event"
    assert AnthropicMessagesTransport.stream_chunk_mode == "line"
    assert _provider().__dict__["stream_chunk_mode"] == "event"
    default = AnthropicMessagesTransport(
        ProviderConfig(api_key="test-key"),
        provider_name=descriptor.provider_id,
        default_base_url=descriptor.default_base_url or "",
    )
    assert default.stream_chunk_mode == "line"
    assert "stream_chunk_mode" not in default.__dict__


def test_subclass_class_default_survives_super_init():
    """OpenRouter keeps its class-level event mode through ``super().__init__``."""
    from repoach.llm_proxy.providers.open_router.client import OpenRouterProvider

    provider = OpenRouterProvider(ProviderConfig(api_key="test-key"))
    assert provider.stream_chunk_mode == "event"
    assert "stream_chunk_mode" not in provider.__dict__


def test_line_chunks_would_hide_the_reply():
    """Pins why the mode matters: line chunks carry no full event."""
    provider = _provider()
    provider.stream_chunk_mode = "line"
    chunks = provider._iter_stream_chunks(_FakeResponse(), state=None, thinking_enabled=False)
    peek = asyncio.run(peek_for_content(chunks))
    assert not peek.got_content
