"""Proxy-only ``repoach/v1`` client.

SP-LLM-NATIVE-FORMAT (2026-05-06): every call from the agent process
goes through the proxy's capability gateway (``POST /v1/agent``)
using the native ``repoach/v1`` shape.  No more OpenAI-format
translation in the middle — :class:`Message`, :class:`ToolSpec`,
:class:`AgentResponse` Pydantic models flow end-to-end.

Public surface preserved (callers keep working):

* :class:`AgentLoop` with ``run_oneshot`` / ``run``
* :class:`ToolDef` (callable + JSON schema), :class:`NimAgentOutput`
* Module-level chain constants (``DEFAULT_REVIEWER_CHAIN``,
  ``PROXY_*_CHAIN`` etc.) — back-compat aliases pointing at the
  matching capability tier.

Internal surface deleted:

* ``OpenAIAdapter``, the ``LLMTier`` registry, the OpenAI-shaped
  ``SimpleNamespace`` translation in ``ProxyGatewayAdapter``.
* All ``openai`` package imports outside ``llm_proxy/providers/``.

The proxy walks the ``MODEL_<CAPABILITY>`` chain from ``.env`` on
every request — all upstream cascade lives on the proxy side.

Module-level constants :
    - :data:`LONG_OUTPUT_TIMEOUT_S` is the per-call timeout for
      long-output personas (the Developer's 32 KB JSON, the
      Coder's full-file rewrites).  Bumped from 420 s to 720 s on
      2026-05-06 (PR #99) after qwen3-coder-480b legitimately ran
      ~9 min wall-clock on a 60 KB diff.  Reviewer / chatbot calls
      override this with a shorter budget per their own
      constructor kwarg.
    - :data:`PROXY_SONNET_CHAIN` and friends are 1-element tuples
      of the matching capability alias kept for back-compat with
      subclasses that still declare ``model_chain =
      PROXY_SONNET_CHAIN``.  They all resolve through
      :func:`alias_for_capability` so renaming the alias on the
      proxy side stays a single point of change.
    - :data:`DEFAULT_REVIEWER_CHAIN` is the base default for the
      reviewers' ``model_chain``, aliased to
      :data:`PROXY_SONNET_CHAIN` so reviewers resolve through the
      full ``MODEL_SONNET`` proxy chain — not a NIM-only chain.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from ..core.config import get_settings
from ..core.logging import get_logger
from ..llm.capability import CapabilityTier, alias_for_capability
from ..llm_proxy.api.models.agent_v1 import (
    AgentResponse,
    Message,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    ToolResultError,
    ToolResultOk,
    ToolSpec,
    TraceEntry,
)
from ..llm_proxy.api.models.anthropic import ThinkingConfig
from .adapters import GatewayChainExhausted, GatewayError, GatewayTransportError, ProxyGatewayClient

_log = get_logger(__name__)

_SEMANTIC_FAILOVER_MAX_ITERATIONS = 20
"""Upper bound on :meth:`AgentLoop.run_oneshot` semantic-failover
attempts (SP-PROXY-SEMANTIC-FAILOVER).  Real configured chains in
``chains.env`` cap at ~12 candidates ; 20 leaves headroom while
preventing a runaway in pathological scenarios where the proxy keeps
returning a candidate the loop already skipped.  The defensive
``rejected in skip_models`` break below terminates earlier in that
case ; this constant is the absolute ceiling."""

LONG_OUTPUT_TIMEOUT_S: float = 2100.0
"""Per-call HTTP read timeout for long-output personas (Developer, Coder).

Sized from live evidence, not optimism (SP-PLAN-FORM dispatch
post-mortem, 2026-06-07): the coder chain head measured ~21 tokens/s
through the proxy, so a full 32 000-token budget needs ~25 minutes of
server-side generation. The previous 720 s value guaranteed that any
dispatch big enough to use the budget timed out client-side while the
proxy kept generating — every retry then stacked another zombie
generation onto the NIM rate limiter until the whole proxy starved
(observed live: three 12-minute ReadTimeouts, then 90 s+ latency on a
one-word completion). 2100 s covers the worst case at ~15 tokens/s
with margin. The step-by-step plan executor (SP-DEV-PLAN-EXEC) will
shrink per-call outputs and make this ceiling comfortable again."""


PROXY_SONNET_CHAIN: tuple[str, ...] = (alias_for_capability(CapabilityTier.SONNET),)
PROXY_OPUS_CHAIN: tuple[str, ...] = (alias_for_capability(CapabilityTier.OPUS),)

DEFAULT_REVIEWER_CHAIN: tuple[str, ...] = PROXY_SONNET_CHAIN


@dataclass(frozen=True)
class ToolDef:
    """JSON-schema description of a callable tool the agent can invoke.

    Attributes:
        name: Identifier the model uses to refer to the tool.
        description: One-sentence purpose; the model reads this to
            decide when to call.
        parameters_schema: JSON schema for the tool args (same
            shape as ``ToolSpec.parameters_schema``).
        callable_fn: Python callable that takes ``**kwargs`` parsed
            from the model's tool call and returns a JSON-serialisable
            result.
    """

    name: str
    description: str
    parameters_schema: dict[str, Any]
    callable_fn: Callable[..., Any]

    def to_tool_spec(self) -> ToolSpec:
        """Serialise as a ``repoach/v1`` :class:`ToolSpec`."""
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters_schema=self.parameters_schema,
        )


@dataclass
class NimAgentOutput:
    """Result of a :class:`AgentLoop` run.

    Attributes:
        text: Final assistant message content (concatenation of
            every :class:`TextBlock` in the last response's
            ``content`` list).
        tool_calls_made: Names of every tool call invoked, in order.
        elapsed_s: Wall-clock duration of the full loop.
        tokens_used: Approximate prompt + completion tokens summed
            across every gateway call.
        model_used: ``model_used`` field reported by the gateway —
            the proxy walked its chain to land on this upstream model.
        turns: Number of round-trips with the model.
        trace: SP-REVIEWER-TRACE-FIELD — the per-candidate chain-failover
            trace the proxy already emits on
            :attr:`~repoach.llm_proxy.api.models.agent_v1.AgentResponse.trace`,
            forwarded here as a list of plain dicts (one entry per
            chain attempt) so downstream callers can serialise it
            without depending on the proxy's pydantic surface.
            Empty when the loop fell through to a transport-level stub.
    """

    text: str = ""
    tool_calls_made: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    tokens_used: int = 0
    model_used: str = ""
    turns: int = 0
    trace: list[dict[str, Any]] = field(default_factory=list)


def _capability_for_alias(alias: str) -> CapabilityTier:
    """Back-compat: classify a free-form alias into a capability tier.

    Used only when a caller passes ``model_chain=`` instead of the
    new ``capability=`` kwarg.  Substring match on the same keywords
    the proxy's ``ModelRouter`` uses; falls back to ``SONNET`` when
    nothing matches.
    """
    lower = alias.lower()
    if "opus" in lower:
        return CapabilityTier.OPUS
    if "haiku" in lower:
        return CapabilityTier.HAIKU
    return CapabilityTier.SONNET


def _trace_to_dicts(trace: list[TraceEntry]) -> list[dict[str, Any]]:
    """SP-REVIEWER-TRACE-FIELD — flatten proxy trace to JSON-safe dicts.

    The proxy emits one :class:`TraceEntry` per chain candidate it
    walked through.  This helper keeps the four operator-actionable
    fields (``attempt`` ordinal, ``model``, ``outcome``, ``elapsed_ms``)
    plus a truncated ``error_message`` for the non-success cases.  The
    ``Usage`` slot is deliberately dropped — token counts per attempt
    bloat the JSON artifacts the CLI emits without changing any
    downstream gate or audit decision.

    Args:
        trace: Per-candidate trace from
            :attr:`AgentResponse.trace`.

    Returns:
        One dict per trace entry, in source order.  Empty list when
        the upstream produced no trace (older proxy versions, stub
        responses, …).
    """
    out: list[dict[str, Any]] = []
    for idx, entry in enumerate(trace):
        error_message = entry.error_message[:200] if entry.error_message else None
        out.append(
            {
                "attempt": idx + 1,
                "model": entry.model,
                "outcome": str(entry.outcome),
                "elapsed_ms": int(entry.elapsed_ms),
                "error_message": error_message,
            }
        )
    return out


def _extract_text(response: AgentResponse) -> str:
    """Concatenate every :class:`TextBlock` in the response content.

    Tool-call blocks are skipped (the loop reads them separately
    via :class:`ToolCallBlock` when deciding the next turn).
    """
    return "".join(b.text for b in response.content if isinstance(b, TextBlock))


_LEAKED_TOOLCALL_MARKUP_RE = re.compile(
    r"\]<\]minimax\[>\[|<\uFF5C+DSML\uFF5C+|<tool_call>|<invoke name="
)
"""Signatures of tool calls leaked as plain text by cheap chain models.

Observed live (SP-ORCH-DOCSTRING, 2026-07-03): minimax-m3 emitted
``]<]minimax[>[<tool_call>`` markup instead of native ``tool_use``
blocks, the loop took it for a final answer, and the session ended
"without writing any file" — four dispatches, ~1.1M tokens, zero
edits. deepseek's DSML variant was seen earlier in persisted coder
summaries. A response matching one of these with no parsed tool_use
is a leaked action, never an answer.
"""


def _extract_tool_calls(response: AgentResponse) -> list[ToolCallBlock]:
    """Pull out every :class:`ToolCallBlock` from the response content."""
    return [b for b in response.content if isinstance(b, ToolCallBlock)]


_MAX_TOOL_ARG_BYTES: int = 16000
"""Upper bound, in UTF-8 bytes, on a serialised tool-call argument payload.

SP-TOOL-DISPATCH-VALIDATE (audit 2026-07-13 finding M16): the loop runs
untrusted chain models, and ``tc.args`` used to be splatted straight
into ``callable_fn`` with no cap of its own — only the tool RESULT was
capped. This bounds the incoming payload before it ever reaches a
callable, independently of that result cap."""

_JSON_SCHEMA_TYPE_PYTHON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
    "null": (type(None),),
}
"""Maps the JSON-Schema ``type`` names the factory's tool schemas
actually use to the Python types :func:`_validate_tool_args` accepts.
Not a full JSON-Schema draft implementation (NG1) — just the subset
:meth:`ToolDef.to_tool_spec` emits."""


def _validate_tool_args(schema: dict[str, Any], args: Any, *, max_bytes: int) -> str | None:
    """Validate model-supplied tool-call args against the declared schema.

    Args:
        schema: the tool's declared ``parameters_schema`` (an object
            schema with ``properties`` and optionally ``required`` /
            ``additionalProperties``, per A1).
        args: the model-supplied ``tc.args`` value to validate.
        max_bytes: cap on the UTF-8 length of ``args`` once serialised.

    Returns:
        ``None`` when ``args`` satisfies the schema and size cap;
        otherwise a human-readable rejection reason.

    A pragmatic validator (NG1): object schemas with typed properties,
    ``required``, and ``additionalProperties`` only — not a
    spec-complete JSON-Schema implementation.
    """
    if not isinstance(args, dict):
        return f"tool args must be a JSON object, got {type(args).__name__}"
    try:
        serialised = json.dumps(args)
    except TypeError as exc:
        return f"tool args are not JSON-serialisable: {exc}"
    if len(serialised.encode("utf-8")) > max_bytes:
        return f"tool args exceed the {max_bytes}-byte cap"

    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    additional_properties = schema.get("additionalProperties", True)

    for key in required:
        if key not in args:
            return f"missing required argument {key!r}"

    if additional_properties is False:
        unknown = sorted(set(args) - set(properties))
        if unknown:
            return f"unknown argument(s) not permitted: {', '.join(unknown)}"

    for key, value in args.items():
        prop_schema = properties.get(key)
        if not isinstance(prop_schema, dict):
            continue
        declared_type = prop_schema.get("type")
        if declared_type is None:
            continue
        declared_type_names = declared_type if isinstance(declared_type, list) else [declared_type]
        python_types: tuple[type, ...] = tuple()
        for type_name in declared_type_names:
            python_types += _JSON_SCHEMA_TYPE_PYTHON_TYPES.get(type_name, ())
        if not python_types:
            continue
        if isinstance(value, bool):
            if "boolean" not in declared_type_names:
                return f"argument {key!r} has wrong type: expected {declared_type}, got bool"
        elif not isinstance(value, python_types):
            return (
                f"argument {key!r} has wrong type: expected "
                f"{declared_type}, got {type(value).__name__}"
            )

    return None


_BUDGET_NUDGE_REMAINING_TURNS: frozenset[int] = frozenset({8, 3})
"""Remaining-turn counts at which the loop warns the model in-band.

Three SP-DEV-STEP-PREFLIGHT dispatches (26, 27, 29 — ~2.6M tokens
combined) each spent every turn exploring and finalized with zero
writes while their own wrap-up summaries stated the implementation
they never issued. The loop is the only party that can see the budget,
so it must say so while turns remain to act on it.
"""


def _budget_nudge_text(remaining: int, max_turns: int) -> str:
    """The in-band budget warning appended alongside tool results."""
    return (
        f"Budget notice: only {remaining} of {max_turns} turns remain before "
        "this loop is finalized. Stop exploring. Produce your deliverables "
        "through tool calls NOW — file writes first if your task requires "
        "them — then give your final answer. Analysis not materialized "
        "through tools before the budget runs out is discarded."
    )


class AgentLoop:
    """Proxy-only ``repoach/v1`` tool-using agent loop."""

    def __init__(
        self,
        *,
        capability: CapabilityTier | None = None,
        model_chain: Iterable[str] | None = None,
        max_turns: int = 15,
        per_call_timeout_s: float = LONG_OUTPUT_TIMEOUT_S,
        max_tokens: int = 1500,
        temperature: float = 0.1,
        thinking: ThinkingConfig | dict[str, Any] | None = None,
    ) -> None:
        """Initialise the loop pointing at the local proxy gateway.

        Args:
            capability: The capability tier the caller wants
                (preferred).  When omitted, ``model_chain`` is read
                for back-compat (its first entry is classified).
            model_chain: Back-compat shim — when ``capability`` is
                ``None``, the first element's substring resolves to
                a tier.  New callers pass ``capability=`` directly.
            max_turns: Hard cap on tool-call round-trips per
                :meth:`run` invocation.
            per_call_timeout_s: HTTP timeout applied to each gateway
                call.  Defaults to :data:`LONG_OUTPUT_TIMEOUT_S`.
            max_tokens: Output token cap per turn.
            temperature: Sampling temperature.
            thinking: Optional thinking config forwarded verbatim to
                every gateway call (tool turns, wrap-up, and wrap-up
                retry).  Absent means today's behaviour (no thinking
                config on the translated request).

        Raises:
            ValueError: If ``model_chain`` is provided but empty,
                or if ``REPOACH_ANTHROPIC_AUTH_TOKEN`` is missing.
        """
        if capability is not None:
            self._capability = capability
        elif model_chain is not None:
            chain = tuple(model_chain)
            if not chain:
                raise ValueError("model_chain must not be empty")
            self._capability = _capability_for_alias(chain[0])
        else:
            self._capability = CapabilityTier.SONNET

        self._model_chain: tuple[str, ...] = (alias_for_capability(self._capability),)

        settings = get_settings()
        secret = settings.llm_proxy_auth_token
        api_key = (
            secret.get_secret_value()
            if secret is not None and hasattr(secret, "get_secret_value")
            else ""
        )
        if not api_key:
            raise ValueError(
                "REPOACH_ANTHROPIC_AUTH_TOKEN is missing — the proxy gateway "
                "requires a shared secret.  Set it in .env."
            )

        base_url = settings.llm_proxy_base_url.rstrip("/")

        self._max_turns = max_turns
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._per_call_timeout_s = per_call_timeout_s
        self._thinking: ThinkingConfig | dict[str, Any] | None = thinking
        self._client = ProxyGatewayClient(
            base_url=base_url,
            api_key=api_key,
            timeout_s=per_call_timeout_s,
        )

    def run_oneshot(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_response: bool = False,
        accept_response: Callable[[AgentResponse], bool] | None = None,
    ) -> NimAgentOutput:
        """Single-turn call without tool use.

        SP-PROXY-SEMANTIC-FAILOVER : when ``accept_response`` is
        provided and rejects a candidate's response, the loop adds
        that candidate's ``model_used`` to a growing skip-list and
        re-calls the proxy with the skip-list propagated via
        :attr:`AgentRequest.skip_models`.  The proxy filters those
        entries from :meth:`ModelRouter.resolve_chain`, so the next
        attempt picks a different upstream candidate.  The loop runs
        at most :data:`_SEMANTIC_FAILOVER_MAX_ITERATIONS` (= 20)
        iterations — a hard ceiling above the longest configured
        chain (~12 candidates in ``chains.env``) — and breaks
        earlier when the proxy returns the same ``model_used`` the
        loop already has in its skip-list (defensive : the proxy
        fell back to its head candidate after every other was
        filtered).  The previous ``raise RuntimeError`` behaviour
        kicks in only after every candidate has been semantically
        rejected or the ceiling is hit.

        Args:
            prompt: User prompt content.
            system: Optional system message.
            json_response: When True, prepend an instruction to the
                prompt so reviewers requesting strict JSON get it.
                ``repoach/v1`` carries no structured-output flag,
                so we honour the contract by promoting the
                instruction at the prompt level.
            accept_response: Optional callback inspecting the
                successful :class:`AgentResponse`.  Returning
                ``False`` triggers a semantic retry (the candidate
                that produced this response is added to the
                skip-list and the proxy walks to the next one).
                After the full chain is exhausted, a
                :class:`RuntimeError` is raised so the outer
                retry-with-backoff (e.g. ``Reviewer._call_with_retry``,
                ``Developer._call_with_retry``) engages.

        Returns:
            A :class:`NimAgentOutput` with one turn and no tool calls.

        Raises:
            RuntimeError: Every candidate in the chain was
                semantically rejected by ``accept_response``.
        """
        effective_prompt = prompt
        if json_response:
            effective_prompt = (
                "Respond with ONLY a single JSON object — no prose, "
                "no markdown fences, no leading commentary.\n\n" + prompt
            )

        messages = [Message(role="user", content=[TextBlock(text=effective_prompt)])]
        skip_models: set[str] = set()
        max_iterations = _SEMANTIC_FAILOVER_MAX_ITERATIONS
        last_response: AgentResponse | None = None
        last_elapsed: float = 0.0
        for iteration in range(1, max_iterations + 1):
            _log.info(
                "agent_loop.semantic_iteration_start",
                capability=self._capability.value,
                iteration=iteration,
                of=max_iterations,
                skip_models=sorted(skip_models),
                gated_by_accept_response=accept_response is not None,
            )
            started = time.monotonic()
            response = self._client.call(
                capability=self._capability,
                system=system,
                messages=messages,
                tools=[],
                max_tokens=self._max_tokens,
                temperature=self._temperature,
                skip_models=frozenset(skip_models),
                thinking=self._thinking,
            )
            last_response = response
            last_elapsed = time.monotonic() - started
            accepted = accept_response is None or accept_response(response)
            _log.info(
                "agent_loop.semantic_iteration_done",
                capability=self._capability.value,
                iteration=iteration,
                model_used=response.model_used or "",
                accepted=accepted,
                elapsed_s=round(last_elapsed, 2),
                output_tokens=int(response.usage.output_tokens),
            )

            if accepted:
                return NimAgentOutput(
                    text=_extract_text(response),
                    tool_calls_made=[],
                    elapsed_s=round(last_elapsed, 2),
                    tokens_used=int(response.usage.total_tokens),
                    model_used=response.model_used or "",
                    turns=1,
                    trace=_trace_to_dicts(response.trace),
                )

            rejected = response.model_used or ""
            _log.warning(
                "agent_loop.semantic_reject",
                capability=self._capability.value,
                iteration=iteration,
                of=max_iterations,
                model_used=rejected,
            )
            if not rejected or rejected in skip_models:
                _log.warning(
                    "agent_loop.semantic_defensive_break",
                    capability=self._capability.value,
                    iteration=iteration,
                    reason="empty_model_used" if not rejected else "duplicate_in_skip_list",
                    rejected=rejected,
                    skip_models=sorted(skip_models),
                )
                break
            skip_models.add(rejected)

        if last_response is not None:
            _log.warning(
                "agent_loop.semantic_chain_exhausted",
                capability=self._capability.value,
                skipped=sorted(skip_models),
            )
        raise RuntimeError(
            f"empty output rejected from capability={self._capability.value} "
            f"after exhausting {len(skip_models)} candidate(s)"
        )

    _TURN_RETRY_BACKOFFS_S: tuple[float, ...] = (0.0, 15.0, 45.0)

    def _call_turn_with_retry(
        self,
        *,
        system: str | None,
        messages: list[Message],
        tool_specs: list[ToolSpec],
        turn: int,
        skip_models: frozenset[str] = frozenset(),
    ) -> AgentResponse:
        """One gateway call with transport-level retry (tool-loop turns).

        SP-PLANNER-AGENT post-mortem: NIM intermittently returns empty
        completions on tool-carrying requests (thinking-budget
        starvation, transient flakiness) and the proxy then answers
        502 ``proxy_chain_exhausted``. A multi-turn exploration dies
        on its first unlucky turn without this — three attempts with
        15/45 s backoffs turn a per-turn coin flip into a reliable
        session, mirroring the Developer's runner-level retry.

        Args:
            system: System prompt for the call.
            messages: Conversation so far.
            tool_specs: Serialised tool surface.
            turn: Loop turn number, for logging only.
            skip_models: Provider-prefixed model refs the proxy must
                bypass this turn (SP-PROXY-SEMANTIC-FAILOVER) — the
                tool loop escalates a repeatedly markup-leaking model
                here so the chain serves the next candidate.

        Returns:
            The gateway response.

        Raises:
            GatewayError: When every attempt failed (re-raised last
                error — semantic failures are never retried here).
        """
        last_exc: GatewayError | None = None
        for attempt, wait_s in enumerate(self._TURN_RETRY_BACKOFFS_S, start=1):
            if wait_s > 0:
                _log.info(
                    "agent_loop.turn_retry_wait",
                    turn=turn,
                    attempt=attempt,
                    wait_s=wait_s,
                    last_error=type(last_exc).__name__ if last_exc else None,
                )
                time.sleep(wait_s)
            try:
                return self._client.call(
                    capability=self._capability,
                    system=system,
                    messages=messages,
                    tools=tool_specs,
                    max_tokens=self._max_tokens,
                    temperature=self._temperature,
                    skip_models=skip_models,
                    thinking=self._thinking,
                )
            except (GatewayTransportError, GatewayChainExhausted) as exc:
                last_exc = exc
                _log.warning(
                    "agent_loop.turn_attempt_failed",
                    turn=turn,
                    attempt=attempt,
                    of=len(self._TURN_RETRY_BACKOFFS_S),
                    error=type(exc).__name__,
                    message=str(exc)[:200],
                )
        assert last_exc is not None
        raise last_exc

    def run(
        self,
        prompt: str,
        *,
        system: str | None = None,
        tools: list[ToolDef] | None = None,
    ) -> NimAgentOutput:
        """Run the tool-using agent loop until the model stops calling tools.

        Args:
            prompt: Initial user prompt.
            system: Optional system message that frames the role.
            tools: Tools the model may invoke.  Empty / ``None``
                degrades to :meth:`run_oneshot`.

        Returns:
            A :class:`NimAgentOutput` with the final assistant message
            and audit metadata.

        On the budget-exhausted path the loop appends one final
        ``user`` turn that asks the model to wrap up without tools,
        using only the evidence already gathered, and returns the
        resulting answer. A wrap-up that leaks tool-call markup is
        retried once with the leaking model skipped — markup can
        never execute once the turns are gone, so it must not become
        the recorded summary. While turns remain, the loop warns the
        model in-band at :data:`_BUDGET_NUDGE_REMAINING_TURNS` so
        deliverables land before the budget dies.
        """
        if not tools:
            return self.run_oneshot(prompt, system=system)

        tools_by_name = {t.name: t for t in tools}
        tool_specs = [t.to_tool_spec() for t in tools]
        messages: list[Message] = [Message(role="user", content=[TextBlock(text=prompt)])]
        tool_calls_log: list[str] = []
        total_tokens = 0
        model_used = ""
        started = time.monotonic()

        _log.info(
            "agent_loop.run_started",
            n_tools_available=len(tools),
            tool_names=[t.name for t in tools],
            max_turns=self._max_turns,
            system_chars=len(system or ""),
            prompt_chars=len(prompt),
            capability=self._capability.value,
        )

        skip_models: set[str] = set()
        consecutive_leaks = 0
        for turn in range(1, self._max_turns + 1):
            _log.debug("agent_loop.turn_start", turn=turn, n_messages=len(messages))
            response = self._call_turn_with_retry(
                system=system,
                messages=messages,
                tool_specs=tool_specs,
                turn=turn,
                skip_models=frozenset(skip_models),
            )
            turn_tokens = int(response.usage.total_tokens)
            total_tokens += turn_tokens
            model_used = response.model_used or model_used

            tool_calls = _extract_tool_calls(response)

            if not tool_calls:
                text = _extract_text(response)
                if _LEAKED_TOOLCALL_MARKUP_RE.search(text):
                    consecutive_leaks += 1
                    _log.warning(
                        "agent_loop.toolcall_markup_leaked",
                        turn=turn,
                        model_used=model_used,
                        consecutive=consecutive_leaks,
                        text_preview=text[:160],
                    )
                    if consecutive_leaks >= 2 and response.model_used:
                        skip_models.add(response.model_used)
                        consecutive_leaks = 0
                        _log.warning(
                            "agent_loop.leaking_model_skipped",
                            turn=turn,
                            skipped=response.model_used,
                            skip_models=sorted(skip_models),
                        )
                    messages.append(Message(role="assistant", content=list(response.content)))
                    messages.append(
                        Message(
                            role="user",
                            content=[
                                TextBlock(
                                    text=(
                                        "Your last reply emitted tool calls as plain "
                                        "text markup, which this loop cannot execute. "
                                        "Re-issue the SAME actions through the native "
                                        "tool interface — one tool_use block per call, "
                                        "never tool syntax inside your text."
                                    )
                                )
                            ],
                        )
                    )
                    continue
                elapsed = round(time.monotonic() - started, 2)
                _log.info(
                    "agent_loop.final_answer",
                    turn=turn,
                    text_chars=len(text),
                    text_preview=text[:120],
                    tool_calls_total=len(tool_calls_log),
                    tokens_used=total_tokens,
                    model_used=model_used,
                    elapsed_s=elapsed,
                )
                return NimAgentOutput(
                    text=text,
                    tool_calls_made=tool_calls_log,
                    elapsed_s=elapsed,
                    tokens_used=total_tokens,
                    model_used=model_used,
                    turns=turn,
                    trace=_trace_to_dicts(response.trace),
                )

            consecutive_leaks = 0
            _log.info(
                "agent_loop.turn_tool_calls",
                turn=turn,
                n_tool_calls=len(tool_calls),
                tool_names=[tc.name for tc in tool_calls],
                model_used=model_used,
                turn_tokens=turn_tokens,
            )

            messages.append(Message(role="assistant", content=list(response.content)))

            result_blocks: list[ToolResultBlock] = []
            for tc in tool_calls:
                tool_name = tc.name
                tool_calls_log.append(tool_name)
                if tool_name not in tools_by_name:
                    _log.warning(
                        "agent_loop.tool_unknown",
                        turn=turn,
                        tool_name=tool_name,
                    )
                    result_blocks.append(
                        ToolResultBlock(
                            tool_call_id=tc.id,
                            result=ToolResultError(
                                ok=False,
                                error={"message": f"unknown tool {tool_name!r}"},
                            ),
                        )
                    )
                    continue
                _log.info(
                    "agent_loop.tool_dispatched",
                    turn=turn,
                    tool_name=tool_name,
                    arg_keys=sorted(tc.args.keys()) if isinstance(tc.args, dict) else [],
                )
                rejection_reason = _validate_tool_args(
                    tools_by_name[tool_name].parameters_schema,
                    tc.args,
                    max_bytes=_MAX_TOOL_ARG_BYTES,
                )
                if rejection_reason is not None:
                    _log.warning(
                        "agent_loop.tool_args_rejected",
                        turn=turn,
                        tool_name=tool_name,
                        reason=rejection_reason,
                    )
                    result_blocks.append(
                        ToolResultBlock(
                            tool_call_id=tc.id,
                            result=ToolResultError(
                                ok=False,
                                error={"message": rejection_reason},
                            ),
                        )
                    )
                    continue
                try:
                    raw = tools_by_name[tool_name].callable_fn(**tc.args)
                    capped = raw if isinstance(raw, str) else json.dumps(raw)
                    capped = capped[:8000]
                    _log.info(
                        "agent_loop.tool_completed",
                        turn=turn,
                        tool_name=tool_name,
                        ok=True,
                        result_chars=len(capped),
                        result_preview=capped[:160],
                    )
                    result_blocks.append(
                        ToolResultBlock(
                            tool_call_id=tc.id,
                            result=ToolResultOk(ok=True, value=capped),
                        )
                    )
                except Exception as exc:
                    _log.error(
                        "agent_loop.tool_completed",
                        turn=turn,
                        tool_name=tool_name,
                        ok=False,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    result_blocks.append(
                        ToolResultBlock(
                            tool_call_id=tc.id,
                            result=ToolResultError(
                                ok=False,
                                error={
                                    "type": type(exc).__name__,
                                    "message": str(exc),
                                },
                            ),
                        )
                    )

            turn_feedback: list[TextBlock | ToolResultBlock] = list(result_blocks)
            remaining_turns = self._max_turns - turn
            if remaining_turns in _BUDGET_NUDGE_REMAINING_TURNS:
                _log.info(
                    "agent_loop.budget_nudge",
                    turn=turn,
                    remaining_turns=remaining_turns,
                    tool_calls_total=len(tool_calls_log),
                )
                turn_feedback.append(
                    TextBlock(text=_budget_nudge_text(remaining_turns, self._max_turns))
                )
            messages.append(Message(role="user", content=turn_feedback))

        _log.warning(
            "agent_loop.budget_exhausted",
            max_turns=self._max_turns,
            tool_calls_total=len(tool_calls_log),
            tool_calls=tool_calls_log[-20:],
        )
        wrap_up_messages = [
            *messages,
            Message(
                role="user",
                content=[
                    TextBlock(
                        text=(
                            "You hit the tool-call budget. Produce your final "
                            "answer now using only the evidence already gathered."
                        )
                    )
                ],
            ),
        ]
        wrap_up = self._client.call(
            capability=self._capability,
            system=system,
            messages=wrap_up_messages,
            tools=[],
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            skip_models=frozenset(skip_models),
            thinking=self._thinking,
        )
        total_tokens += int(wrap_up.usage.total_tokens)
        wrap_up_text = _extract_text(wrap_up)
        if _LEAKED_TOOLCALL_MARKUP_RE.search(wrap_up_text):
            _log.warning(
                "agent_loop.wrapup_markup_leaked",
                model_used=wrap_up.model_used,
                text_preview=wrap_up_text[:160],
            )
            if wrap_up.model_used:
                skip_models.add(wrap_up.model_used)
            wrap_up = self._client.call(
                capability=self._capability,
                system=system,
                messages=[
                    *wrap_up_messages,
                    Message(role="assistant", content=list(wrap_up.content)),
                    Message(
                        role="user",
                        content=[
                            TextBlock(
                                text=(
                                    "Your last reply emitted tool-call markup as "
                                    "plain text. No tools can run anymore. State "
                                    "your final answer as prose only."
                                )
                            )
                        ],
                    ),
                ],
                tools=[],
                max_tokens=self._max_tokens,
                temperature=self._temperature,
                skip_models=frozenset(skip_models),
                thinking=self._thinking,
            )
            total_tokens += int(wrap_up.usage.total_tokens)
            wrap_up_text = _extract_text(wrap_up)
        return NimAgentOutput(
            text=wrap_up_text,
            tool_calls_made=tool_calls_log,
            elapsed_s=round(time.monotonic() - started, 2),
            tokens_used=total_tokens,
            model_used=wrap_up.model_used or model_used,
            turns=self._max_turns,
            trace=_trace_to_dicts(wrap_up.trace),
        )


__all__ = [
    "DEFAULT_REVIEWER_CHAIN",
    "LONG_OUTPUT_TIMEOUT_S",
    "PROXY_OPUS_CHAIN",
    "PROXY_SONNET_CHAIN",
    "AgentLoop",
    "GatewayChainExhausted",
    "GatewayError",
    "GatewayTransportError",
    "NimAgentOutput",
    "ToolDef",
]
