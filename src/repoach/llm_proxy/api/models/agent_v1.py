"""Pydantic models for the ``repoach/v1`` capability gateway shape.

Schema authority: ``docs/specs/2026-05-05_SP-CAPABILITY-FORMAT_response_shape.md``.

The shape is provider-agnostic and forms the contract between the
capability gateway endpoint (``POST /v1/agent``) and every repoach
client (WA chat, review bots, data-analyst, future agents).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .anthropic import ThinkingConfig


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ToolCallBlock(BaseModel):
    """Assistant emits this when it wants the client to dispatch a tool."""

    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class ToolResultOk(BaseModel):
    ok: Literal[True] = True
    value: Any


class ToolResultError(BaseModel):
    ok: Literal[False] = False
    error: dict[str, Any]


class ToolResultBlock(BaseModel):
    """Client emits this on the next turn to feed the tool output back."""

    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    result: ToolResultOk | ToolResultError = Field(discriminator="ok")


ContentBlock = TextBlock | ToolCallBlock | ToolResultBlock


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters_schema: dict[str, Any]


class Message(BaseModel):
    """One conversational turn.

    ``system`` lives at top level on :class:`AgentRequest` only —
    embedding it inside ``messages[]`` is rejected by the underlying
    Anthropic schema and by the dispatcher's translator.  The role
    enum is kept strict so the mistake is caught at request
    validation time.
    """

    role: Literal["user", "assistant"]
    content: list[ContentBlock]


class AgentRequest(BaseModel):
    schema_version: Literal["1"] = "1"
    capability: Literal["opus", "sonnet", "haiku"]
    system: str | None = None
    messages: list[Message]
    tools: list[ToolSpec] = Field(default_factory=list)
    max_tokens: int | None = None
    temperature: float = 0.1
    skip_models: list[str] = Field(default_factory=list)
    """SP-PROXY-SEMANTIC-FAILOVER — provider-prefixed model refs the
    caller has already tried and semantically rejected on this same
    retry chain.  The proxy filters them out of
    :meth:`ModelRouter.resolve_chain` so the next attempt picks a
    different candidate.  Empty list (default) means no filtering —
    the resolve falls through to the full configured chain."""
    thinking: ThinkingConfig | None = None
    """SP-AGENT-THINKING-CONTROL — optional thinking control mirroring
    :class:`MessagesRequest.thinking`.  Absent (default) means today's
    behaviour: the dispatcher does not set a thinking config on the
    translated request and the provider's global default applies.
    When set, the dispatcher copies the value verbatim onto the
    built :class:`MessagesRequest` so the existing per-provider
    reasoning machinery takes over (NIM bounds it, OpenRouter
    honours it, etc.)."""


StopReason = Literal["end_turn", "tool_use", "max_turns", "content_filter", "error"]


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int
    total_tokens: int
    reasoning_tokens: int = 0
    """Attribution only — already included in ``output_tokens`` (NG3).

    Defaults to 0 so existing callers building ``Usage(...)`` without
    this field keep working; a missing or unreadable upstream detail
    also resolves to 0 rather than failing the response.
    """


TraceOutcome = Literal[
    "ok",
    "skipped_text_contract_no_tools",
    "transport_error",
    "connection_error",
    "timeout",
    "content_filter",
    "disguised_error",
    "unknown_error",
]


class TraceEntry(BaseModel):
    model: str
    outcome: TraceOutcome
    elapsed_ms: int
    usage: Usage | None = None
    error_message: str | None = None


class AgentError(BaseModel):
    code: Literal[
        "chain_exhausted",
        "provider_filter",
        "invalid_capability",
        "invalid_request",
    ]
    message: str
    trace_summary: str | None = None


class AgentResponse(BaseModel):
    """Outcome of one capability-gateway turn.

    ``model_used`` is ``None`` only when ``stop_reason == "error"``
    (no provider answered).  The default-``None`` value lets dumps
    with ``exclude_none=True`` round-trip cleanly.
    """

    schema_version: Literal["1"] = "1"
    capability: Literal["opus", "sonnet", "haiku"]
    stop_reason: StopReason
    model_used: str | None = None
    content: list[TextBlock | ToolCallBlock] = Field(default_factory=list)
    usage: Usage
    elapsed_ms: int
    trace: list[TraceEntry] = Field(default_factory=list)
    error: AgentError | None = None

    @model_validator(mode="after")
    def _enforce_error_invariants(self) -> AgentResponse:
        if self.stop_reason == "error":
            if self.error is None:
                raise ValueError("stop_reason=error requires an `error` object")
            if self.model_used is not None:
                raise ValueError("stop_reason=error implies model_used=null")
        if self.stop_reason == "content_filter" and self.error is None:
            raise ValueError("stop_reason=content_filter requires an `error` object")
        return self
