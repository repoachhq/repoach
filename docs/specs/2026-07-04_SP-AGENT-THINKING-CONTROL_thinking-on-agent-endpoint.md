---
id: SP-AGENT-THINKING-CONTROL
title: Thinking control on the /v1/agent endpoint
version: 0.1
status: approved
author: jfaye (thinking-handling audit, 2026-07-04)
created: 2026-07-04
updated: 2026-07-04

owns:
  code: [src/repoach/llm_proxy/api/agent_dispatcher.py]
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Thinking control on the /v1/agent endpoint

## Intent

Give `/v1/agent` callers the same thinking control `/v1/messages`
callers already have. The entire factory (reviewers, Developer,
Planner, judge) runs through `/v1/agent`, which today runs every turn
with thinking forced to the global default and no per-request budget,
effort, or off-switch — while `agent_loop.py`'s own comments document
NIM empty completions on tool-carrying requests attributed to
thinking-budget starvation.

## Context

`MessagesRequest` (`api/models/anthropic.py`) carries a
`ThinkingConfig` that every provider's request builder honours
(enable/disable and budget bounds per the `REASONING_CONTROLS` matrix
in `providers/reasoning.py`). `AgentRequest`
(`api/models/agent_v1.py:72-87`) has no thinking field, and
`_translate_request` (`api/agent_dispatcher.py`) never sets one on the
`MessagesRequest` it builds — so the per-provider thinking machinery
is unreachable for agent traffic. The client side (`AgentLoop` /
`ProxyGatewayClient` in `src/ferova/agent_engine/`) likewise has no
way to express a thinking preference.

## Goals

- G1: `AgentRequest` gains an optional `thinking` field with the same
  shape `/v1/messages` accepts (`{"type": "enabled", "budget_tokens":
  N}` or `{"type": "disabled"}`); absent means exactly today's
  behaviour (global default).
- G2: `_translate_request` copies the field verbatim onto the built
  `MessagesRequest`, putting the existing per-provider thinking
  machinery in charge from there.
- G3: The agent_engine client (`ProxyGatewayClient.call` in
  `src/ferova/agent_engine/adapters.py` and `AgentLoop.__init__` /
  `run` plumbing) accepts an optional thinking config and sends it
  with every turn of a loop; the default for every existing caller is
  unchanged (no field sent).
- G4: The wrap-up call and refinement turns inherit the loop's
  thinking config (one policy per loop, not per turn).

## Non-Goals

- NG1: No policy decision in this slice — no role gets a new thinking
  setting; all callers keep the default. Choosing budgets per role
  (Developer tool turns vs judge) is an operator decision after
  SP-USAGE-REASONING-SPLIT provides numbers.
- NG2: No change to the per-provider reasoning matrix or budgets.
- NG3: No `/v1/messages` change.

## Assumptions

- A1: `src/ferova/llm_proxy/api/agent_dispatcher.py` is unowned in the
  arch registry (verified 2026-07-04: `owner_of` returns `None`);
  secondary touched files (`api/models/agent_v1.py`,
  `src/ferova/agent_engine/adapters.py`,
  `src/ferova/agent_engine/agent_loop.py`) stay frontier.
- A2: A defaulted optional field on `AgentRequest` is
  backward-compatible: requests from older clients simply omit it.

## Interface

Inputs:
- `AgentRequest.thinking: ThinkingConfig | None = None` — optional,
  same schema as the Anthropic messages field.
- `AgentLoop(..., thinking: dict | None = None)` /
  `ProxyGatewayClient.call(..., thinking=None)` — optional
  pass-through on the client side.

Outputs: N/A (request plumbing).

Errors:
- Request validation error (422) for a malformed thinking object —
  same validation `/v1/messages` applies via `ThinkingConfig`.

## Behavior

### Nominal

A caller sends `thinking={"type": "enabled", "budget_tokens": 1024}`
on `/v1/agent`; the translated `MessagesRequest` carries it; NIM's
request builder bounds it per its own rules, OpenRouter honours it,
exactly as for `/v1/messages` traffic.

### Edge cases

- Field absent → translated request carries no thinking config;
  provider behaviour is byte-identical to today.
- `{"type": "disabled"}` → providers that support an off-switch
  disable reasoning; others strip reasoning output client-side
  (existing semantics).

### Failure scenarios

- Malformed thinking payload → 422 at request validation, no
  dispatch.

## Architecture Impact

- No edge added or removed. `agent_dispatcher.py` moves from the
  frontier into this spec's `owns.code`.

## Diagram

N/A (one optional field threaded through two layers).

## Acceptance Criteria

- [ ] AC1: `tests/unit/test_agent_thinking_control.py::test_thinking_field_reaches_the_translated_request`
  — an `AgentRequest` with an enabled thinking config produces a
  `MessagesRequest` carrying the identical config.
- [ ] AC2: `tests/unit/test_agent_thinking_control.py::test_absent_thinking_field_translates_to_none`
  — no field → the translated request's thinking is `None` (today's
  behaviour pinned).
- [ ] AC3: `tests/unit/test_agent_thinking_control.py::test_disabled_thinking_round_trips`
  — `{"type": "disabled"}` survives translation intact.
- [ ] AC4: `tests/unit/test_agent_thinking_control.py::test_agent_loop_threads_thinking_to_every_turn`
  — an `AgentLoop` constructed with a thinking config sends it on
  tool turns AND on the budget-exhausted wrap-up call (scripted
  client records kwargs).
- [ ] AC5: The full unit suite passes.

## Open Questions

(none)
