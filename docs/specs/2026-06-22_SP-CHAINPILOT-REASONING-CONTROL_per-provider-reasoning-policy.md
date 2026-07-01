---
id: SP-CHAINPILOT-REASONING-CONTROL
title: Per-provider reasoning-control policy (the headroom framework)
version: 0.1
status: approved
author: agent
created: 2026-06-22
updated: 2026-06-22

owns:
  code: [src/ferova/llm_proxy/providers/reasoning.py]   # the pure reasoning-control policy module
  resources: N/A                                            # pure logic; no shared state

depends_on: []                                              # stdlib only; transports consume it (frontier)
provides_to: []                                             # AUTO-maintained

constraints: {}
---

# SP-CHAINPILOT-REASONING-CONTROL — per-provider reasoning-control policy

## Intent
Phase 0b-1 of the Chain Autopilot arc. A single pure module encoding the
**verified** per-provider reasoning-control matrix and the headroom policy,
so a thinking model leaves room for its answer. Transports consume it
(0b-2/0b-3); this slice is the framework only — additive, unwired, fully
unit-tested.

## Context
Phase 0a (`docs/chain_autopilot_thinking_audit.md`) showed the failover spine
already copes with budget-starvation (peek + retry). The remaining work is
*headroom*. A live doc-verified audit (2026-06-22) of all six providers'
reasoning APIs established that the knob is **heterogeneous**, and that only
two providers expose a real reasoning-token budget:

| Provider | Knob | `max_tokens` shared with reasoning? |
|----------|------|--------------------------------------|
| nvidia_nim | token-budget (`chat_template_kwargs.reasoning_budget`) | no (separate) |
| open_router | token-budget (`reasoning.max_tokens`) | no |
| kimi | toggle (`thinking.type`) | yes |
| groq | effort (`reasoning_effort`) | yes |
| cerebras | effort (`reasoning_effort`) | yes |
| deepseek | toggle + effort (`thinking`, `reasoning_effort` high/max) | yes |

The four direct providers cannot reserve answer headroom per request; their
levers are to lower effort or rely on a generous `max_tokens` + the existing
budget-retry. This module encodes that reality once, transport-agnostically.

## Goals
- G1: `bounded_reasoning_budget(max_tokens) -> int | None` — the headroom math
  lifted from `nvidia_nim/request.py:_bounded_reasoning_budget` (half the
  budget, capped at 2048, floored at 256, `None` when unusable), so every
  transport reuses one definition.
- G2: `KnobType` enum (`TOKEN_BUDGET` / `EFFORT` / `TOGGLE` / `NONE`) and a
  frozen `ReasoningControl` descriptor (knob, `min_effort`, `combined_budget`)
  + a `REASONING_CONTROLS` table keyed by provider id, encoding the matrix.
- G3: `plan_reasoning(provider_id, *, max_tokens, thinking_enabled) ->
  ReasoningPlan` — a pure policy returning the abstract decision
  (`token_budget` / `effort` / `thinking_enabled` / `answer_headroom_floor`),
  which the transports translate into wire fields later. Unknown provider →
  the conservative `NONE` plan.

## Non-Goals
- NG1: Wires NO transport — `nvidia_nim` / `open_router` / the generic path
  are untouched (that is 0b-2/0b-3). Purely additive.
- NG2: Does NOT move the per-provider table onto `ProviderDescriptor` (a later
  refactor may); it lives in `reasoning.py` for now.
- NG3: Does NOT change `chains.env` or pick models.

## Assumptions
- A1: The verified matrix (2026-06-22) is current; provider APIs move, so the
  table is expected to be revised — it is centralised here precisely so a
  change is one edit.

## Interface
`src/ferova/llm_proxy/providers/reasoning.py`:

- `bounded_reasoning_budget(max_tokens: Any, *, cap: int = 2048) -> int | None`
- `class KnobType(StrEnum)`: `TOKEN_BUDGET`, `EFFORT`, `TOGGLE`, `NONE`
- `@dataclass(frozen=True) ReasoningControl`: `knob: KnobType`,
  `min_effort: str | None = None`, `combined_budget: bool = True`
- `REASONING_CONTROLS: dict[str, ReasoningControl]` — nvidia_nim, open_router,
  kimi, groq, cerebras, deepseek
- `@dataclass(frozen=True) ReasoningPlan`: `token_budget: int | None`,
  `effort: str | None`, `thinking_enabled: bool`, `answer_headroom_floor: int | None`
- `plan_reasoning(provider_id: str, *, max_tokens: int | None, thinking_enabled: bool) -> ReasoningPlan`

## Behavior

### Nominal
- `thinking_enabled=False` → a plan that disables reasoning where the knob
  allows (toggle/effort `none`-capable) and sets no budget.
- TOKEN_BUDGET provider + thinking on → `token_budget = bounded_reasoning_budget(max_tokens)`.
- EFFORT provider + thinking on → `effort = control.min_effort`,
  `answer_headroom_floor` set so a combined `max_tokens` keeps answer room.
- TOGGLE-only provider + thinking on → `thinking_enabled=True`, no budget/effort,
  `answer_headroom_floor` set (rely on retry).

### Edge cases
- `max_tokens` unusable (`None`/≤0) → `token_budget=None`; floor omitted.
- Unknown `provider_id` → `KnobType.NONE` plan (no fields), safe default.

### Failure scenarios
- N/A — pure function; raises nothing on valid types.

## Architecture Impact
- New leaf `providers/reasoning.py`; `depends_on: []` (stdlib only).
- New / changed coupling, cycles, shared state: none. Transports become
  frontier consumers in 0b-2/0b-3.

## Diagram
N/A — single pure module.

## Acceptance Criteria
- [ ] AC1: `bounded_reasoning_budget` matches the lifted behaviour (caps at
  2048, halves, floors at 256, `None` on unusable) — pinned by tests mirroring
  `test_nim_reasoning_budget`.
- [ ] AC2: `REASONING_CONTROLS` encodes the six providers with the verified
  knob types; a test asserts each entry's knob.
- [ ] AC3: `plan_reasoning` returns a `token_budget` for nvidia_nim/open_router,
  an `effort` (= `min_effort`) for groq/cerebras/deepseek, and a toggle-only
  plan for kimi — each with thinking on; and a reasoning-disabled plan with
  thinking off.
- [ ] AC4: an unknown provider id yields the `NONE` plan (no exception).
- [ ] AC5: the module is unwired — no import added to any transport in this
  slice (grep proves transports unchanged).

## Open Questions
- None.
</content>
