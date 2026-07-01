---
id: SP-CHAINPILOT-REASONING-WIRE-TOKEN
title: Wire the token-budget transports to the shared reasoning helper
version: 0.1
status: approved
author: agent
created: 2026-06-22
updated: 2026-06-22

owns:
  code:
    - src/ferova/llm_proxy/providers/nvidia_nim/request.py    # NIM request builder (token-budget transport)
    - src/ferova/llm_proxy/providers/open_router/request.py   # OpenRouter request builder (token-budget transport)
  resources: N/A

depends_on: [SP-CHAINPILOT-REASONING-CONTROL]   # both builders import the shared bounded_reasoning_budget
provides_to: []                                  # AUTO-maintained

constraints: {}
---

# SP-CHAINPILOT-REASONING-WIRE-TOKEN — wire the token-budget transports

## Intent
Phase 0b-2 of the Chain Autopilot arc. Route the two transports that expose a
real reasoning-token budget — NIM and OpenRouter — through the shared
`bounded_reasoning_budget` (SP-CHAINPILOT-REASONING-CONTROL), so the headroom
math has one definition and OpenRouter gets a default reasoning bound it
lacks today.

## Context
0b-1 created `providers/reasoning.py` with `bounded_reasoning_budget` (lifted
from NIM's private `_bounded_reasoning_budget`) and the verified matrix:
nvidia_nim and open_router are the only `TOKEN_BUDGET` providers. NIM already
bounds reasoning via `chat_template_kwargs.reasoning_budget` using its own
private helper; OpenRouter (`open_router/request.py:_apply_reasoning`) sets
`reasoning.max_tokens` ONLY when the client supplied `thinking.budget_tokens`,
so a thinking request without a client budget gets no bound. The effort/toggle
transports (kimi/groq/cerebras/deepseek via the generic path) are 0b-3.

## Goals
- G1: NIM's `build_request_body` uses the shared `bounded_reasoning_budget`;
  its private `_bounded_reasoning_budget` and `_REASONING_BUDGET_CAP` are
  removed. NIM behaviour is byte-identical (same math, same field).
- G2: OpenRouter applies a DEFAULT reasoning bound — when the client did not
  set `thinking.budget_tokens`, `reasoning.max_tokens` defaults to
  `bounded_reasoning_budget(max_tokens)`. An explicit client budget still wins.
- G3: One definition of the headroom math across both token-budget transports.

## Non-Goals
- NG1: Does NOT touch the generic OpenAI path (kimi/groq/cerebras/deepseek) or
  `claude_code` — that is 0b-3.
- NG2: Does NOT change NIM's observable request body (refactor only).
- NG3: Does NOT change failover/peek/retry logic.

## Assumptions
- A1: `reasoning.py` (owned by SP-CHAINPILOT-REASONING-CONTROL) is the canonical
  home of `bounded_reasoning_budget`; importing it is the declared edge.

## Interface
- `nvidia_nim/request.py`: `from ...providers.reasoning import bounded_reasoning_budget`;
  `_bounded_reasoning_budget` / `_REASONING_BUDGET_CAP` deleted.
- `open_router/request.py`: `_apply_reasoning(body, thinking_cfg)` sets a
  default `reasoning.max_tokens = bounded_reasoning_budget(body["max_tokens"])`
  when no client `budget_tokens` is present.

## Behavior

### Nominal
- NIM thinking request: `chat_template_kwargs.reasoning_budget` =
  `bounded_reasoning_budget(max_tokens)` — identical to before.
- OpenRouter thinking request, no client budget: `reasoning.max_tokens` =
  `bounded_reasoning_budget(max_tokens)` (new default bound).
- OpenRouter thinking request WITH client `budget_tokens`: that value still
  wins (`setdefault`).

### Edge cases
- `max_tokens` unusable → `bounded_reasoning_budget` returns `None` → the
  field is omitted (both transports), unchanged from today.

### Failure scenarios
- N/A — request shaping only.

## Architecture Impact
- Adds dependency: `SP-CHAINPILOT-REASONING-WIRE-TOKEN` →
  `SP-CHAINPILOT-REASONING-CONTROL` (both builders import
  `bounded_reasoning_budget`). The arc's first cross-governed edge — the
  edge-honesty gate verifies it.
- New / changed coupling, cycles, shared state: none beyond that edge.

## Diagram
N/A — two request builders consume one helper.

## Acceptance Criteria
- [ ] AC1: NIM's request body for a thinking request carries
  `extra_body.chat_template_kwargs.reasoning_budget == bounded_reasoning_budget(max_tokens)`;
  `_bounded_reasoning_budget` no longer exists in `nvidia_nim/request.py`.
- [ ] AC2: OpenRouter's body for a thinking request with no client budget
  carries `reasoning.max_tokens == bounded_reasoning_budget(max_tokens)`.
- [ ] AC3: OpenRouter still honours an explicit client `thinking.budget_tokens`
  (it is not overwritten by the default).
- [ ] AC4: the existing NIM + OpenRouter + failover suites stay green; the NIM
  body is byte-identical to before this slice.
- [ ] AC5: `ferova arch check` resolves the import edge against the declared
  `depends_on` (gate green).

## Open Questions
- None.
</content>
