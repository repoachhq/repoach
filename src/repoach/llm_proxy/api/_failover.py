"""Chain-failover helpers for the proxy dispatcher (SP-PROXY-CHAIN-FAILOVER).

The proxy iterates the resolved chain when streaming a response: each
candidate provider is tried in turn until one yields meaningful content
within the peek window.  This module isolates the SSE event inspection
so the policy can be unit-tested without touching the FastAPI service.

Failure modes considered transient and triggering failover:

- Exceptions during ``stream_response`` startup or first-event yield —
  including ``httpx.TransportError`` / ``TimeoutException`` /
  ``RemoteProtocolError`` and OpenAI-style ``APIConnectionError``.
- A completed stream whose final ``message_delta.usage.output_tokens``
  is zero.  This catches both the silent NIM transport-error symptom
  (``RemoteProtocolError: Server disconnected without sending a
  response`` → empty completion) **and** the variant where the NIM
  provider disguises the connection error as a fake text content block
  (``content_block_delta`` with ``text_delta = "Connection error.
  (request_id=…)"``) — observed live on 2026-05-04 17:31.  In both
  cases the upstream produced zero billable tokens, so any text we
  saw is a synthetic placeholder, not real model output.
- A non-zero ``stop_reason`` of ``"error"`` reported by any
  ``message_delta``.
- A completed stream whose accumulated ``text_delta`` content is
  empty or whitespace-only **even when** ``output_tokens > 0``
  (SP-PROXY-FAILOVER-WHITESPACE — observed live 2026-05-21 17:09
  on PR #175 Sentinel pass : Kimi K2.6 returned a single space
  ``" "`` with ``output_tokens=1`` ; the previous rules treated
  that as success, but the downstream reviewer's parser strips
  whitespace and classifies the response as TRANSPORT failure
  anyway).  Whitespace-only text carries no information — the
  upstream is effectively silent — so the chain failover must
  take over rather than letting the empty reply reach the caller.

Failure modes **not** triggering failover (genuine model output):

- A ``content_block_start`` of type ``tool_use`` (the model picked a
  tool — that's what we wanted).  Note : the tool-use shortcut wins
  unconditionally ; the whitespace check applies only to
  text-only completions where ``tool_use`` was never seen.
- A ``message_delta.usage.output_tokens > 0`` after the stream
  completes **and** at least one non-whitespace character was
  emitted via ``content_block_delta.text_delta``.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

from loguru import logger


class FirstByteTimeoutError(Exception):
    """Raised when a stream's first SSE chunk misses its deadline.

    Distinct from the transport-level ``http_read_timeout``
    (``providers/base.py``), which only bounds the gap between two
    already-started reads: this error fires when a hop accepts the
    connection and then sends nothing at all, a state otherwise
    indistinguishable from a legitimately slow-but-alive stream until
    the full read timeout elapses.
    """


@dataclass(frozen=True, slots=True)
class PeekResult:
    """Outcome of inspecting an SSE event stream for content.

    Attributes:
        buffered: All events buffered from the stream, in order.  When
            :attr:`got_content` is ``True`` the caller forwards these
            events to the client; when ``False`` they are discarded
            and the next chain candidate is tried.
        got_content: ``True`` if the stream represents a successful
            upstream response — either a ``tool_use`` content block
            was emitted, or the final ``message_delta`` reported a
            non-zero ``usage.output_tokens``.
        stream_done: ``True`` once the stream emitted ``message_stop``.
            ``False`` when :func:`peek_for_content` exited early after a
            per-chunk terminal-error signal fired (``stop_reason == "error"``
            on a ``message_delta``, or the documented disguised-error text
            pattern) — in that case the remaining chunks were never read and
            the caller's ``stream`` was explicitly closed via ``aclose()``
            instead of drained to completion.
        looks_budget_starved: ``True`` when ``got_content`` is ``False``
            because the stream completed normally but produced no usable
            text (``output_tokens`` was zero, or the text was
            whitespace-only) — the signature of a model whose hidden
            reasoning consumed the whole ``max_tokens`` budget.
            ``False`` for a transport/error empty, which must not be
            retried.  The dispatcher retries a budget-starved candidate
            with a larger budget before failing over
            (SP-PROXY-THINKING-BUDGET-RETRY).
        final_output_tokens: The ``usage.output_tokens`` from the final
            ``message_delta`` emitted by the stream, if any.
            ``None`` when no ``message_delta`` arrived before
            ``message_stop`` (or stream exhaustion).  Exposed so
            callers can compute tokens-per-second for slow-completion
            policies without re-parsing the buffered stream.
        upstream_status_code: The real upstream HTTP status code recovered
            from the terminal ``message_delta``'s ``error_status_code``
            field, when the transport carried one through
            (SP-BREAKER-LIVE-REASONS).  ``None`` for a genuinely
            content-less, non-errored stream, or when the transport could
            not recover a status — callers must fall back to the generic
            ``empty_completion`` reason in that case, never worse than
            before this field existed.
    """

    buffered: list[str]
    got_content: bool
    stream_done: bool
    looks_budget_starved: bool = False
    final_output_tokens: int | None = None
    upstream_status_code: int | None = None


_EVENT_TYPE_PATTERN = re.compile(r"^event:\s*(\S+)", re.MULTILINE)
_DATA_LINE_PATTERN = re.compile(r"^data:\s*(.*)$", re.MULTILINE)
_DISGUISED_ERROR_TEXT_PATTERN = re.compile(r"^Connection error\.\s*\(request_id=")


def _parse_event(chunk: str) -> tuple[str | None, dict | None]:
    """Extract ``(event_type, data_obj)`` from a single SSE chunk.

    Returns ``(None, None)`` for chunks that do not contain a parseable
    event/data pair (heartbeats, partial buffers, comments).
    """
    type_match = _EVENT_TYPE_PATTERN.search(chunk)
    data_match = _DATA_LINE_PATTERN.search(chunk)
    if type_match is None or data_match is None:
        return None, None
    raw = data_match.group(1).strip()
    if not raw or raw == "[DONE]":
        return type_match.group(1), None
    try:
        return type_match.group(1), json.loads(raw)
    except json.JSONDecodeError:
        return type_match.group(1), None


def _is_tool_use_start(event_type: str | None, data: dict | None) -> bool:
    if event_type != "content_block_start" or data is None:
        return False
    block = data.get("content_block") or {}
    return block.get("type") == "tool_use"


def _message_delta_usage(event_type: str | None, data: dict | None) -> dict | None:
    if event_type != "message_delta" or data is None:
        return None
    usage = data.get("usage")
    return usage if isinstance(usage, dict) else None


def _message_delta_error_status(event_type: str | None, data: dict | None) -> int | None:
    if event_type != "message_delta" or data is None:
        return None
    status = data.get("error_status_code")
    return status if isinstance(status, int) else None


def _message_delta_stop_reason(event_type: str | None, data: dict | None) -> str | None:
    if event_type != "message_delta" or data is None:
        return None
    delta = data.get("delta") or {}
    reason = delta.get("stop_reason")
    return reason if isinstance(reason, str) else None


def _marks_stream_end(event_type: str | None) -> bool:
    return event_type == "message_stop"


def _text_delta(event_type: str | None, data: dict | None) -> str | None:
    if event_type != "content_block_delta" or data is None:
        return None
    delta = data.get("delta") or {}
    if delta.get("type") != "text_delta":
        return None
    text = delta.get("text")
    return text if isinstance(text, str) else None


def chunk_is_tool_use_start(chunk: str) -> bool:
    """Return whether an SSE chunk is a ``content_block_start`` for a
    ``tool_use`` block.  These cannot be faked by transport-error
    handlers (NIM disguises errors as text, never as tool_use), so
    they are an unambiguous success signal."""
    event_type, data = _parse_event(chunk)
    return _is_tool_use_start(event_type, data)


def chunk_message_delta_usage(chunk: str) -> dict | None:
    """Return the ``usage`` payload of a terminal ``message_delta`` chunk.

    Returns ``None`` for any other event type or unparseable chunk.
    """
    event_type, data = _parse_event(chunk)
    return _message_delta_usage(event_type, data)


def chunk_message_delta_error_status(chunk: str) -> int | None:
    """Return the recovered upstream HTTP status of a terminal ``message_delta``.

    ``None`` for any other event type, an unparseable chunk, or a
    ``message_delta`` whose ``error_status_code`` field is absent or not
    an integer — the transport only sets this field when it caught a
    real upstream exception it could map to a status
    (SP-BREAKER-LIVE-REASONS).
    """
    event_type, data = _parse_event(chunk)
    return _message_delta_error_status(event_type, data)


def chunk_message_delta_stop_reason(chunk: str) -> str | None:
    """Return the ``stop_reason`` of a terminal ``message_delta`` chunk."""
    event_type, data = _parse_event(chunk)
    return _message_delta_stop_reason(event_type, data)


def chunk_marks_stream_end(chunk: str) -> bool:
    """Return whether a chunk is a normal ``message_stop`` terminator."""
    event_type, _ = _parse_event(chunk)
    return _marks_stream_end(event_type)


def chunk_text_delta(chunk: str) -> str | None:
    """Return the text payload of a ``content_block_delta`` text chunk.

    The helper isolates the SSE event inspection so callers (notably
    :func:`peek_for_content`) can accumulate text content across the
    stream without re-implementing the parse + type-check chain.

    Args:
        chunk: A single SSE chunk as produced by the upstream
            provider's ``stream_response`` iterator.  Heartbeats,
            partial buffers, and comments are accepted and yield
            ``None``.

    Returns:
        The literal ``text`` field of the inner delta when the chunk
        is a ``content_block_delta`` whose ``delta.type`` is
        ``"text_delta"``.  May be the empty string when the upstream
        emitted an empty ``text`` field — callers should treat
        empty / whitespace-only payloads as carrying no real
        information (SP-PROXY-FAILOVER-WHITESPACE).
        Returns ``None`` for any other event type, unparseable
        chunks, or deltas of a different type (e.g.
        ``input_json_delta`` for tool calls).
    """
    event_type, data = _parse_event(chunk)
    return _text_delta(event_type, data)


def text_is_disguised_error(text: str) -> bool:
    """Return whether accumulated ``text_delta`` content opens with the
    documented NIM disguised-connection-error signature.

    Matches the literal shape observed live on 2026-05-04 17:31 (module
    docstring above): a synthetic ``"Connection error. (request_id=…)"``
    text block a broken upstream emits in place of a real completion.
    Any other text, including partial prefixes still arriving, returns
    ``False``.
    """
    return _DISGUISED_ERROR_TEXT_PATTERN.match(text) is not None


async def peek_for_content(
    stream: AsyncIterator[str],
    *,
    first_byte_deadline_s: float | None = None,
) -> PeekResult:
    """Drain the stream and decide whether it carried real content.

    Reads chunks until ``message_stop``, the iterator is exhausted, or
    a per-chunk terminal-error signal fires, buffering them so the
    caller can replay them on success.

    Decision rule (in order):

    1. If any ``message_delta`` carried ``stop_reason == "error"``,
       the stream is a failure regardless of any prior content. This
       signal is checked per-chunk and breaks the drain the instant it
       fires, without waiting for ``message_stop``.
    2. If the accumulated ``text_delta`` content matches
       :func:`text_is_disguised_error`, the stream is a failure. Also
       checked per-chunk and breaks the drain immediately — this is
       the common case for the disguised NIM connection error, which
       previously required draining to the terminal ``message_delta``
       to see ``output_tokens == 0``.
    3. If any chunk is a ``content_block_start`` of type ``tool_use``,
       the response is real (NIM never disguises errors as tool_use).
    4. Otherwise, the final ``message_delta.usage.output_tokens`` is
       authoritative: ``> 0`` means real model output ; ``0`` (or no
       ``message_delta`` at all) means transient failure — even if
       earlier ``content_block_*`` events appeared, those are the
       provider's synthetic error placeholder.
    5. **SP-PROXY-FAILOVER-WHITESPACE** : when ``output_tokens > 0``
       *and* no ``tool_use`` block was seen, the accumulated
       ``text_delta`` content must contain at least one
       non-whitespace character.  Otherwise the upstream produced
       only whitespace — observed live with Kimi K2.6 returning
       ``" "`` on PR #175 Sentinel — which carries no information
       and must trigger failover.

    Buffering the full response loses streaming-as-tokens-arrive on
    successful first attempts, but the wa_chat / agent traffic that
    motivated this dispatcher does not display tokens incrementally,
    and correctness on the failover path matters more than that
    perceived latency.

    Args:
        stream: Async iterator yielding raw SSE chunks from the
            current chain candidate's ``stream_response`` call.  The
            iterator must terminate (either via ``message_stop`` or
            natural exhaustion) ; long-running streams without a
            terminator are not supported by this peek window.
        first_byte_deadline_s: Bounds the wait for the stream's FIRST
            chunk, distinct from the transport-level
            ``http_read_timeout`` that bounds gaps between reads once
            streaming has started. ``None`` or any value ``<= 0``
            disables enforcement (byte-for-byte identical to the
            behavior before this parameter existed). SP-PROXY-FIRST-BYTE-DEADLINE.

    Returns:
        A :class:`PeekResult` carrying the buffered events (so the
        caller can replay them on success), a ``got_content``
        boolean computed per the decision rule above, and
        ``stream_done`` reflecting whether ``message_stop`` was
        reached (``False`` on an early terminal-error abort — see
        :attr:`PeekResult.stream_done`).

    Raises:
        FirstByteTimeoutError: ``first_byte_deadline_s`` elapsed before
            the stream yielded a single chunk. An immediately-exhausted
            stream (``StopAsyncIteration`` on the very first
            ``__anext__()``, itself within the deadline) is NOT this
            error — it falls through to the ordinary empty-completion
            decision rules below.
    """
    buffered: list[str] = []
    saw_tool_use = False
    final_output_tokens: int | None = None
    saw_error_stop_reason = False
    saw_disguised_error_text = False
    exited_on_terminal_error = False
    accumulated_text: list[str] = []
    stream_done = False
    upstream_status_code: int | None = None

    def _absorb(chunk: str) -> bool:
        nonlocal saw_tool_use, final_output_tokens, saw_error_stop_reason
        nonlocal saw_disguised_error_text, exited_on_terminal_error, upstream_status_code
        buffered.append(chunk)
        event_type, data = _parse_event(chunk)
        if _is_tool_use_start(event_type, data):
            saw_tool_use = True
        usage = _message_delta_usage(event_type, data)
        if usage is not None:
            tokens = usage.get("output_tokens")
            if isinstance(tokens, int):
                final_output_tokens = tokens
        error_status = _message_delta_error_status(event_type, data)
        if error_status is not None:
            upstream_status_code = error_status
        stop_reason = _message_delta_stop_reason(event_type, data)
        if stop_reason == "error":
            saw_error_stop_reason = True
            exited_on_terminal_error = True
            return False
        text = _text_delta(event_type, data)
        if text is not None:
            accumulated_text.append(text)
            if text_is_disguised_error("".join(accumulated_text)):
                saw_disguised_error_text = True
                exited_on_terminal_error = True
                return False
        return _marks_stream_end(event_type)

    stream_iter = stream.__aiter__()
    if first_byte_deadline_s is not None and first_byte_deadline_s > 0:
        try:
            first_chunk = await asyncio.wait_for(
                stream_iter.__anext__(), timeout=first_byte_deadline_s
            )
        except StopAsyncIteration:
            stream_done = True
        except TimeoutError as exc:
            raise FirstByteTimeoutError(
                f"no SSE chunk within first_byte_deadline_s={first_byte_deadline_s}"
            ) from exc
        else:
            if _absorb(first_chunk):
                stream_done = True

    if not stream_done and not exited_on_terminal_error:
        async for chunk in stream_iter:
            if _absorb(chunk):
                stream_done = True
                break
            if exited_on_terminal_error:
                break

    if exited_on_terminal_error:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            await aclose()

    text_buffer = "".join(accumulated_text)
    text_chars = len(text_buffer)
    text_strip_chars = len(text_buffer.strip())

    if saw_error_stop_reason:
        got_content = False
        decision_reason = "stop_reason_error"
    elif saw_disguised_error_text:
        got_content = False
        decision_reason = "disguised_error_text"
    elif saw_tool_use:
        got_content = True
        decision_reason = "tool_use"
    elif final_output_tokens is None or final_output_tokens == 0:
        got_content = False
        decision_reason = "output_tokens_zero"
    elif text_strip_chars == 0:
        got_content = False
        decision_reason = "whitespace_only"
    else:
        got_content = True
        decision_reason = "real_text"

    looks_budget_starved = (
        not got_content
        and stream_done
        and decision_reason in ("output_tokens_zero", "whitespace_only")
    )

    logger.info(
        "PEEK_DECISION: got_content={} reason={} budget_starved={} output_tokens={} "
        "text_chars={} text_strip_chars={} text_preview={!r}",
        got_content,
        decision_reason,
        looks_budget_starved,
        final_output_tokens,
        text_chars,
        text_strip_chars,
        text_buffer[:80],
    )
    return PeekResult(
        buffered=buffered,
        got_content=got_content,
        stream_done=stream_done,
        looks_budget_starved=looks_budget_starved,
        final_output_tokens=final_output_tokens,
        upstream_status_code=upstream_status_code,
    )
