"""Unit tests for SP-PROXY-FIRST-BYTE-DEADLINE.

Pins ``peek_for_content``'s new ``first_byte_deadline_s`` keyword: a
stalling stream that never yields a first chunk is aborted well under
its deadline with a ``FirstByteTimeoutError``, an immediately-exhausted
stream is NOT mistaken for a timeout, and ``_classify_failover_reason``
tells the two apart from a generically-named timeout exception.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest

from repoach.llm_proxy.api._failover import FirstByteTimeoutError, peek_for_content
from repoach.llm_proxy.api.services import _classify_failover_reason


async def _stalling_stream(sleep_s: float) -> AsyncIterator[str]:
    await asyncio.sleep(sleep_s)
    yield "event: message_stop\ndata: {}\n\n"


async def _empty_stream() -> AsyncIterator[str]:
    return
    yield ""


def test_peek_for_content_raises_first_byte_timeout_when_no_chunk_arrives() -> None:
    """A fake provider that sleeps 30 s before its first chunk is aborted in well under 2 s."""

    async def _run() -> float:
        started = time.monotonic()
        with pytest.raises(FirstByteTimeoutError):
            await peek_for_content(_stalling_stream(30.0), first_byte_deadline_s=0.05)
        return time.monotonic() - started

    elapsed = asyncio.run(_run())
    assert elapsed < 2.0, f"expected abort well under 2s, took {elapsed}s"


def test_peek_for_content_treats_immediate_exhaustion_as_empty_completion_not_timeout() -> None:
    """A stream that exhausts on its very first ``__anext__()`` is empty completion, not timeout."""

    async def _run() -> None:
        result = await peek_for_content(_empty_stream(), first_byte_deadline_s=5.0)
        assert result.got_content is False
        assert result.stream_done is True
        assert result.final_output_tokens is None

    asyncio.run(_run())


def test_first_byte_timeout_classified_distinctly_from_generic_timeout() -> None:
    """``_classify_failover_reason`` returns a distinct reason for ``FirstByteTimeoutError``."""

    class _GenericTimeoutError(Exception):
        pass

    assert _classify_failover_reason(FirstByteTimeoutError("no chunk")) == "first_byte_timeout"
    assert _classify_failover_reason(_GenericTimeoutError("slow upstream")) == "timeout"
