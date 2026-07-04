"""Capability gateway dispatcher (SP-CAPABILITY-GATEWAY).

Translates an incoming :class:`AgentRequest` (``ferova/v1`` shape)
into the Anthropic-style :class:`MessagesRequest` consumed by the
existing proxy pipeline, runs it through
:class:`ClaudeProxyService.create_message`, consumes the resulting
SSE stream, and translates the aggregated message back into an
:class:`AgentResponse`.

The agentic *loop* (multi-turn tool_use ↔ tool_result) is **not** in
scope here — this dispatcher is single-turn-stateless. Multi-turn
orchestration lives in the Python SDK
(``ferova.llm.gateway.run_agent_loop``).
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from loguru import logger

from ...llm.capability import CAPABILITY_TO_ALIAS
from .models.agent_v1 import (
    AgentError,
    AgentRequest,
    AgentResponse,
    StopReason,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    ToolResultError,
    ToolResultOk,
    ToolSpec,
    TraceEntry,
    Usage,
)
from .models.agent_v1 import (
    Message as AgentMessage,
)
from .models.anthropic import (
    ContentBlockText,
    ContentBlockToolResult,
    ContentBlockToolUse,
    MessagesRequest,
)
from .models.anthropic import (
    Message as AnthropicMessage,
)
from .models.anthropic import (
    Tool as AnthropicTool,
)
from .services import ClaudeProxyService

_CAPABILITY_TO_ANTHROPIC_MODEL: dict[str, str] = {
    capability.value: alias for capability, alias in CAPABILITY_TO_ALIAS.items()
}


_ANTHROPIC_TO_AGENT_STOP: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_turns",
    "stop_sequence": "end_turn",
    "refusal": "content_filter",
}


async def dispatch_agent_request(
    request: AgentRequest,
    service: ClaudeProxyService,
) -> AgentResponse:
    """Run a single capability-gateway turn and return a :class:`AgentResponse`.

    Args:
        request: ``ferova/v1`` request body.
        service: The existing proxy service that knows how to walk the
            chain stored in MODEL_<CAPABILITY>.

    Returns:
        A populated :class:`AgentResponse`.
    """
    started_ms = _now_ms()
    capability_model = _CAPABILITY_TO_ANTHROPIC_MODEL[request.capability]

    try:
        messages_request = _translate_request(request, capability_model)
    except ValueError as exc:
        return _error_response(
            request=request,
            code="invalid_request",
            message=str(exc),
            started_ms=started_ms,
        )

    response_obj = service.create_message(
        messages_request,
        skip_models=frozenset(request.skip_models),
    )
    aggregated = await _aggregate_sse_stream(response_obj.body_iterator)
    elapsed_ms = _now_ms() - started_ms
    model_used = aggregated.get("model") or capability_model
    stop_reason = _ANTHROPIC_TO_AGENT_STOP.get(aggregated.get("stop_reason") or "", "end_turn")
    usage = aggregated["usage"]
    content = _translate_content_blocks(aggregated["content_blocks"])

    text_blocks = [b for b in content if isinstance(b, TextBlock)]
    tool_blocks = [b for b in content if isinstance(b, ToolCallBlock)]
    logger.info(
        (
            "AGENT_DISPATCH model={} stop_reason={} elapsed_ms={} "
            "tokens={} n_text_blocks={} n_tool_calls={} text_preview={!r} "
            "tool_calls={}"
        ),
        model_used,
        stop_reason,
        elapsed_ms,
        usage.total_tokens,
        len(text_blocks),
        len(tool_blocks),
        ("\n".join(b.text for b in text_blocks))[:400],
        [{"name": tc.name, "id": tc.id, "args_preview": str(tc.args)[:200]} for tc in tool_blocks],
    )

    return AgentResponse(
        schema_version="1",
        capability=request.capability,
        stop_reason=stop_reason,
        model_used=model_used,
        content=content,
        usage=usage,
        elapsed_ms=elapsed_ms,
        trace=[
            TraceEntry(
                model=model_used,
                outcome="ok",
                elapsed_ms=elapsed_ms,
                usage=usage,
            )
        ],
    )


def _translate_request(request: AgentRequest, model_alias: str) -> MessagesRequest:
    """Build the Anthropic-style request the existing service consumes."""
    return MessagesRequest(
        model=model_alias,
        max_tokens=request.max_tokens or 1500,
        messages=[_translate_message(m) for m in request.messages],
        system=request.system,
        stream=True,
        temperature=request.temperature,
        tools=[_translate_tool(t) for t in request.tools] or None,
        thinking=request.thinking,
    )


def _translate_message(message: AgentMessage) -> AnthropicMessage:
    """Translate a ferova/v1 message → Anthropic message."""
    role = message.role
    blocks: list[Any] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            blocks.append(ContentBlockText(type="text", text=block.text))
        elif isinstance(block, ToolCallBlock):
            blocks.append(
                ContentBlockToolUse(
                    type="tool_use",
                    id=block.id,
                    name=block.name,
                    input=block.args,
                )
            )
        elif isinstance(block, ToolResultBlock):
            blocks.append(
                ContentBlockToolResult(
                    type="tool_result",
                    tool_use_id=block.tool_call_id,
                    content=_serialise_tool_result(block.result),
                )
            )
        else:
            raise ValueError(f"unknown block type {type(block).__name__}")
    return AnthropicMessage(role=role, content=blocks)


def _serialise_tool_result(result: ToolResultOk | ToolResultError) -> str:
    """Encode a typed tool_result into the string Anthropic expects."""
    if isinstance(result, ToolResultOk):
        return json.dumps({"ok": True, "value": result.value})
    return json.dumps({"ok": False, "error": result.error})


def _translate_tool(tool: ToolSpec) -> AnthropicTool:
    return AnthropicTool(
        name=tool.name,
        description=tool.description,
        input_schema=tool.parameters_schema,
    )


async def _aggregate_sse_stream(
    body_iterator: AsyncIterator[bytes | str],
) -> dict[str, Any]:
    """Walk the Anthropic SSE stream and collect the final message.

    Returns a dict with keys:
        * ``content_blocks``: list of ``{type, text|name|id|input_json}`` entries
        * ``stop_reason``: Anthropic stop_reason string (may be empty)
        * ``model``: provider/model id (proxy decorates ``message_start``
          with the resolved model alias)
        * ``usage``: aggregated :class:`Usage` (best effort — sums
          token counts seen across ``message_start`` + ``message_delta``)
    """
    blocks_by_index: dict[int, dict[str, Any]] = {}
    stop_reason: str = ""
    model: str = ""
    input_tokens = 0
    output_tokens = 0
    reasoning_tokens = 0

    async for chunk in body_iterator:
        text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
        for raw_event in _split_sse_events(text):
            data = _parse_sse_data(raw_event)
            if not data:
                continue
            event_type = data.get("type") or ""
            if event_type == "message_start":
                msg = data.get("message", {}) or {}
                model = str(msg.get("model") or "") or model
                usage = msg.get("usage") or {}
                input_tokens += int(usage.get("input_tokens") or 0)
                output_tokens += int(usage.get("output_tokens") or 0)
            elif event_type == "content_block_start":
                idx = int(data.get("index") or 0)
                cb = data.get("content_block") or {}
                blocks_by_index[idx] = {
                    "type": cb.get("type"),
                    "id": cb.get("id"),
                    "name": cb.get("name"),
                    "text": "",
                    "input_json": "",
                }
            elif event_type == "content_block_delta":
                idx = int(data.get("index") or 0)
                delta = data.get("delta") or {}
                target = blocks_by_index.setdefault(
                    idx,
                    {"type": None, "id": None, "name": None, "text": "", "input_json": ""},
                )
                if delta.get("type") == "text_delta":
                    target["text"] += str(delta.get("text") or "")
                elif delta.get("type") == "input_json_delta":
                    target["input_json"] += str(delta.get("partial_json") or "")
            elif event_type == "message_delta":
                delta = data.get("delta") or {}
                if delta.get("stop_reason"):
                    stop_reason = str(delta.get("stop_reason"))
                usage = data.get("usage") or {}
                output_tokens += int(usage.get("output_tokens") or 0)
                reasoning_tokens += int(data.get("reasoning_tokens") or 0)

    ordered_blocks = [blocks_by_index[i] for i in sorted(blocks_by_index)]
    return {
        "content_blocks": ordered_blocks,
        "stop_reason": stop_reason,
        "model": model,
        "usage": Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            reasoning_tokens=reasoning_tokens,
        ),
    }


def _split_sse_events(text: str) -> list[str]:
    """Split a chunk of SSE text into individual ``event:.../data:...`` blocks."""
    return [block.strip() for block in text.split("\n\n") if block.strip()]


def _parse_sse_data(raw_event: str) -> dict[str, Any] | None:
    """Extract the JSON ``data:`` payload from one SSE event block."""
    for line in raw_event.splitlines():
        if line.startswith("data:"):
            payload = line[len("data:") :].strip()
            if not payload or payload == "[DONE]":
                return None
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                logger.debug("agent_dispatcher.sse_parse_skip payload={}", payload[:120])
                return None
            return parsed if isinstance(parsed, dict) else None
    return None


def _translate_content_blocks(blocks: list[dict[str, Any]]) -> list[Any]:
    """Translate raw aggregated blocks → typed ferova/v1 content.

    Unknown block types (``thinking``, ``redacted_thinking``, …) are
    silently dropped because they don't fit the ``ferova/v1``
    vocabulary and never carry actionable signal for the client.
    """
    out: list[Any] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type == "text":
            text = str(block.get("text") or "")
            out.append(TextBlock(type="text", text=text))
        elif block_type == "tool_use":
            args_raw = block.get("input_json") or "{}"
            try:
                args = json.loads(args_raw) if args_raw else {}
            except json.JSONDecodeError:
                args = {"_raw_args": args_raw}
            out.append(
                ToolCallBlock(
                    type="tool_call",
                    id=str(block.get("id") or ""),
                    name=str(block.get("name") or ""),
                    args=args,
                )
            )
    return out


def _now_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def _error_response(
    *,
    request: AgentRequest,
    code: str,
    message: str,
    started_ms: int,
) -> AgentResponse:
    """Build an `error`-typed AgentResponse for invalid_request paths."""
    elapsed_ms = _now_ms() - started_ms
    return AgentResponse(
        schema_version="1",
        capability=request.capability,
        stop_reason="error",
        model_used=None,
        content=[],
        usage=Usage(input_tokens=0, output_tokens=0, total_tokens=0),
        elapsed_ms=elapsed_ms,
        trace=[],
        error=AgentError(code=code, message=message),
    )
