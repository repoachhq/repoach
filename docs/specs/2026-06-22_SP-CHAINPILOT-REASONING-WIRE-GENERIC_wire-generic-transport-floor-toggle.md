---
id: SP-CHAINPILOT-REASONING-WIRE-GENERIC
title: Wire the generic transport to the reasoning plan (floor + toggle; effort deferred)
version: 0.1
status: approved
author: agent
created: 2026-06-22
updated: 2026-06-22

owns:
  code: [src/ferova/llm_proxy/providers/openai_generic.py]   # the generic OpenAI transport
  resources: N/A

depends_on:
  - SP-CHAINPILOT-REASONING-CONTROL   # imports plan_reasoning
  - SP-CHAINPILOT-EFFORT-MAP          # get_effort_map — 2a-3-iv-b closed the effort deferral
  - SP-CHAINPILOT-EFFORT-KNOB         # effort_extra_body — the reasoning_effort wire fragment
  - SP-USAGE-REASONING-SPLIT          # imports providers.openai_compat
provides_to: []                                  # AUTO-maintained

constraints: {}
---

# SP-CHAINPILOT-REASONING-WIRE-GENERIC — wire the generic transport (floor + toggle)

# (was provisionally 0b-3 "WIRE-EFFORT"; renamed — effort is deferred to Phase 2)

## Intent
Phase 0b-3 (final Phase-0 slice). Apply the reasoning plan
(SP-CHAINPILOT-REASONING-CONTROL) in the generic OpenAI transport that serves
kimi / groq / cerebras / deepseek — but ONLY the universally-safe parts: the
`max_tokens` headroom floor and the thinking-disable toggle on the two
providers that verifiably accept it. The per-model `reasoning_effort` knob is
**deferred to Phase 2** (see Decision).

## Context
0b-2 wired the token-budget transports (NIM, OpenRouter). The four direct
providers go through `GenericOpenAIProvider` (`openai_generic.py`), which today
emits a plain OpenAI body with no reasoning fields. The verified matrix
([[provider-reasoning-knobs]], `providers/reasoning.py`) says these providers
share `max_tokens` between reasoning and answer (combined budget), so the only
safe per-request lever is a generous `max_tokens` floor; thinking can be
disabled via `thinking:{type:"disabled"}` on kimi and deepseek (doc-verified).

## Decision — effort deferred to Phase 2 (CLOSED by 2a-3-iv-b)
> **Update (2026-06-24): this deferral is now closed.** SP-CHAINPILOT-EFFORT-APPLY
> (2a-3-iv-b) wires `reasoning_effort` into `_apply_reasoning_plan`, gated by the
> runtime effort map (the probe matrix resolved which cells reason at which
> effort). The original reasoning below stands as the historical record; the
> "NOT applied" statements in Goals/Non-Goals/Acceptance below describe the 0b-3
> snapshot, superseded by 2a-3-iv-b.

`reasoning_effort` is **per-model, not per-provider** (groq/cerebras: gpt-oss
takes `low/medium/high` but qwen takes `none/default`; deepseek floor is
`high`). We do not yet know which model occupies each cell, and the generic
transport is **not currently routed by any chain**, so applying a
provider-level effort would be speculative and untestable. The
`ReasoningPlan.effort` field is therefore intentionally NOT applied here. The
Phase 2 probe matrix learns which (provider, model) cells reason and which
effort value each accepts; the per-model effort wiring lands then.

## Goals
- G1: `GenericOpenAIProvider._build_request_body` calls
  `plan_reasoning(self._provider_name, max_tokens, thinking_enabled)` and
  applies: the `answer_headroom_floor` to `max_tokens`, and (thinking off only,
  kimi/deepseek only) `extra_body.thinking = {"type": "disabled"}`.
- G2: `plan.effort` is NOT applied (deferred). A docstring states why.
- G3: A non-reasoning / unknown provider is unaffected (NONE plan → no fields).

## Non-Goals
- NG1: Does NOT apply `reasoning_effort` (Phase 2).
- NG2: Does NOT touch NIM / OpenRouter / claude_code.
- NG3: Does NOT add any provider to a chain.

## Assumptions
- A1: `self._provider_name` is the catalog provider id (kimi/groq/cerebras/deepseek).
- A2: kimi + deepseek accept `thinking:{type:"disabled"}` (doc-verified 2026-06-22).

## Interface
`openai_generic.py`:
- `_build_request_body(request)` — base body + `_apply_reasoning_plan`.
- `_apply_reasoning_plan(body, plan)` — floor + toggle-disable (kimi/deepseek);
  effort omitted.
- `_TOGGLE_DISABLE_PROVIDERS = frozenset({"kimi", "deepseek"})`.

## Behavior

### Nominal
- thinking on, combined-budget provider → `max_tokens` raised to the floor if
  below it; no `reasoning_effort` emitted.
- thinking off, kimi/deepseek → `extra_body.thinking = {"type": "disabled"}`.

### Edge cases
- thinking off, groq/cerebras → no disable field (disable is effort-based and
  per-model → deferred); body unchanged but for the (absent) floor.
- unknown provider → NONE plan → body unchanged.
- `max_tokens` already ≥ floor → left as-is.

### Failure scenarios
- N/A — request shaping only.

## Architecture Impact
- Adds dependency: `SP-CHAINPILOT-REASONING-WIRE-GENERIC` →
  `SP-CHAINPILOT-REASONING-CONTROL` (imports `plan_reasoning`). Gate-verified.
- New / changed coupling, cycles, shared state: none beyond that edge.

## Diagram
N/A — one transport consumes the plan.

## Acceptance Criteria
- [ ] AC1: a thinking-on request to a combined-budget provider (e.g. kimi) has
  `max_tokens >= answer_headroom_floor`.
- [ ] AC2: a thinking-off request to kimi/deepseek carries
  `extra_body.thinking == {"type": "disabled"}`; to groq/cerebras it does not.
- [ ] AC3: no request carries `reasoning_effort` (deferred) — asserted.
- [ ] AC4: an unknown provider id leaves the body free of reasoning fields.
- [ ] AC5: the budget-retry e2e path (`test_proxy_budget_retry`) stays green —
  the starved→retry→success spine that carries a thinking model is unchanged
  (0c coverage).

## Open Questions
- None. (Effort deferral is a Decision, not an open question — tracked in the
  umbrella Phase 2 and [[chain-autopilot-arc]].)
</content>
