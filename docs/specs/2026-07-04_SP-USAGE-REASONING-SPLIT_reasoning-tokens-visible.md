---
id: SP-USAGE-REASONING-SPLIT
title: Reasoning tokens visible in usage accounting
version: 0.1
status: approved
author: jfaye (thinking-handling audit, 2026-07-04)
created: 2026-07-04
updated: 2026-07-04

owns:
  code: [src/repoach/llm_proxy/providers/openai_compat.py]
  resources: []

depends_on: [SP-PROVIDER-TRANSPORT-SPI, SP-BUDGET-RETRY-FIXES, SP-PROVIDER-INIT-DEDUP]
provides_to: []

constraints: {}
---

# Reasoning tokens visible in usage accounting

## Intent

Make reasoning tokens a first-class, separately-reported number in the
proxy's usage accounting instead of an invisible share of
`output_tokens` — today no layer of the stack can say what fraction of
an ~880k-token Developer dispatch was reasoning, so every downstream
decision about thinking (budgets, chain ordering, model eviction)
flies blind.

## Context

Three layers currently erase the split. (1) Upstream read:
`openai_compat.py` reads only `usage.completion_tokens` and
`usage.prompt_tokens` from OpenAI-compatible providers and never reads
`completion_tokens_details.reasoning_tokens`, so provider-billed
reasoning is folded into output. (2) Estimation fallback:
`estimate_output_tokens` in `core/anthropic/sse.py` explicitly
computes a reasoning-token estimate from the accumulated thinking
text, ADDS it into the single output estimate, then discards the
split. (3) Schema: `Usage` in `api/models/agent_v1.py` carries exactly
`input_tokens`/`output_tokens`/`total_tokens`, and
`api/agent_dispatcher.py` builds it as input+output.

The `/v1/messages` response keeps its Anthropic-compatible usage shape
untouched — external callers must not see a schema change there. The
new field surfaces on the internal `/v1/agent` schema only, which
ferova's own `AgentLoop` consumes.

## Goals

- G1: The openai_chat transport reads
  `usage.completion_tokens_details.reasoning_tokens` when the upstream
  payload carries it (tolerating its absence and `None`), and carries
  the value through to the response metadata alongside the existing
  input/output counts.
- G2: The SSE estimation fallback preserves the reasoning/text split
  it already computes internally: when real usage is absent, the
  reasoning share of the estimate is exposed instead of being summed
  away.
- G3: `Usage` (agent_v1 schema) gains `reasoning_tokens: int = 0`, and
  the `/v1/agent` dispatcher populates it; `total_tokens` semantics
  are unchanged (reasoning stays included in output_tokens as today —
  the new field is attribution, not re-bucketing).
- G4: `/v1/messages` responses are byte-compatible with today (no new
  field in the Anthropic-shaped usage).

## Non-Goals

- NG1: No persistence change (no new DB column; audit threading is a
  later slice once the number exists at the boundary).
- NG2: No behaviour change driven by the number (no budget or chain
  decisions) — measurement only.
- NG3: No re-bucketing: `output_tokens` keeps including reasoning, as
  billed by providers.
- NG4: No OpenRouter-native-path changes (its thinking blocks pass
  through; token detail extraction there is a later slice).

## Assumptions

- A1: `src/ferova/llm_proxy/providers/openai_compat.py` is unowned in
  the arch registry (verified 2026-07-04: `owner_of` returns `None`),
  so this spec may claim it; secondary touched files
  (`core/anthropic/sse.py`, `api/models/agent_v1.py`,
  `api/agent_dispatcher.py`) stay frontier (ownership governs
  boundaries, not working sets).
- A2: Adding a defaulted field to the agent_v1 `Usage` model is
  backward-compatible for its only consumer, `agent_engine/adapters.py`
  (pydantic ignores unknown fields on older clients; new field has a
  default for old payloads).

## Interface

Inputs: N/A (no new public API; existing upstream payloads).

Outputs:
- `Usage.reasoning_tokens: int` — 0 when the upstream reports nothing
  and no thinking text was accumulated.

Errors: none raised — a malformed or missing
`completion_tokens_details` never fails a response (fall back to 0).

## Behavior

### Nominal

A reasoning model served via NIM returns
`completion_tokens_details.reasoning_tokens=1200`; the `/v1/agent`
response carries `usage.reasoning_tokens == 1200` while
`output_tokens` is unchanged from today.

### Edge cases

- Upstream omits `completion_tokens_details` (or sets it `null`) →
  `reasoning_tokens == 0`.
- Usage absent entirely → the SSE estimator's reasoning share is used.
- Thinking disabled → 0 (no thinking accumulated, no detail field).

### Failure scenarios

- Upstream sends a non-integer detail value → treated as absent
  (0), one debug log, response unaffected.

## Architecture Impact

- `openai_compat.py` moves from the frontier into this spec's
  `owns.code`.
- Adds dependency: SP-USAGE-REASONING-SPLIT -> SP-PROVIDER-TRANSPORT-SPI
  (`openai_compat.py` imports `providers.error_mapping`, owned there).
- Adds dependency: SP-USAGE-REASONING-SPLIT -> SP-BUDGET-RETRY-FIXES
  (the working-set file `api/agent_dispatcher.py` imports
  `api.services`, owned there since the 2026-07-04 thinking specs).

## Diagram

N/A (accounting field threading).

## Acceptance Criteria

- [ ] AC1: `tests/unit/test_proxy_usage_reasoning_split.py::test_upstream_reasoning_detail_is_surfaced`
  — a fake openai_chat stream whose final usage carries
  `completion_tokens_details.reasoning_tokens` yields an agent_v1
  `Usage` with that value in `reasoning_tokens` and unchanged
  `output_tokens`.
- [ ] AC2: `tests/unit/test_proxy_usage_reasoning_split.py::test_missing_detail_defaults_to_zero`
  — the same stream without the detail field yields
  `reasoning_tokens == 0`.
- [ ] AC3: `tests/unit/test_proxy_usage_reasoning_split.py::test_estimator_split_survives_when_usage_absent`
  — no upstream usage, accumulated thinking text present → the
  estimator's reasoning share lands in `reasoning_tokens`.
- [ ] AC4: `tests/unit/test_proxy_usage_reasoning_split.py::test_messages_usage_shape_unchanged`
  — a `/v1/messages` response payload contains no `reasoning_tokens`
  key in its usage object.
- [ ] AC5: The full unit suite passes.

## Open Questions

(none)
