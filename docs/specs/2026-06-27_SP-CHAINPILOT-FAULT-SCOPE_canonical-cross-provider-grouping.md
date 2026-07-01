---
id: SP-CHAINPILOT-FAULT-SCOPE
title: MODEL_FAULT only when bad on ALL providers — canonical cross-provider grouping
version: 0.1
status: draft
author: agent
created: 2026-06-27
updated: 2026-06-27

owns:
  code: N/A
  resources: N/A

depends_on:
  - SP-CHAINPILOT-ATTRIBUTION    # amends the classifier (3a)
  - SP-CHAINPILOT-EQUIVALENCES   # canonical resolver used for the grouping
  - SP-CHAINPILOT-LOOP           # the loop now passes equivalences into attribute_faults

provides_to: []
constraints: {}
---

# SP-CHAINPILOT-FAULT-SCOPE — a model healthy on its provider is never evicted model-wide

## Intent
Fix-3 of four after the 2026-06-27 armed-autopilot incident — the deepest root
cause of the wrong evictions. `attribute_faults` grouped cells for the
model-vs-provider split by the **raw provider model id**. The same model is
exposed under provider-specific ids (`deepseek-ai/deepseek-v4-pro` on NIM,
`deepseek/deepseek-v4-pro` on OpenRouter, `deepseek-v4-pro` direct), so every cell
looked single-provider: a model healthy on its own provider but failing on
another (e.g. an OpenRouter free tier) was classified `MODEL_FAULT` (evict
everywhere) instead of `PROVIDER_FAULT` (drop just the failing provider ref). The
incident evicted `deepseek-v4-pro` — `ok×4` on `deepseek` — from the chains.

## Context
The split's correctness hinges on the cross-provider *grouping*, and the
adversarial review showed the equivalence join alone is not enough — the table's
substring matching both over-merged distinct SKUs (broad `deepseek-v4` pattern
collapsed `deepseek-v4-flash` onto `deepseek-v4-pro`, both live in `MODEL_CODER`)
and under-covered variant ids (`mistral-small-2603` → unmatched), each re-creating
the misattribution class. So this fix has three layers:

1. **Canonical grouping** — `_model_key(model_id, equivalences)` groups cells by
   canonical (1d) when a table is supplied (raw id otherwise); `attribute_faults`
   gains an optional `equivalences` parameter, passed by `plan_and_apply` (3e).
   Each `CellFault` keeps its raw provider/model id (so the decision engine drops
   the exact ref) — only the *grouping* is canonical.
2. **A `≥2 distinct providers` guard on `MODEL_FAULT`** — the robust safety net.
   A model-wide eviction now requires the model observed dead on **two or more
   distinct providers**; a single observed provider yields only `PROVIDER_FAULT`
   (drop that ref). So however imperfect the equivalence join, it can never
   *over-evict* on one provider's evidence (the incident) — at worst it
   under-evicts (drop instead of evict), which is low-harm, capped (Fix-1), and
   recurs next cycle.
3. **Tighter matching** — `_match_model_id` now picks the **longest** (most
   specific) matching pattern, and the broad `deepseek-v4` id_pattern is removed,
   so sibling SKUs are not collapsed.

Without a table (`equivalences=None`) grouping falls back to the raw id, so prior
call sites are unaffected except where the `≥2 providers` guard makes a
single-provider failure a `PROVIDER_FAULT` (drop) instead of `MODEL_FAULT` — which
removes the same ref for a single-provider model.

## Goals
- G1: `_model_key` groups cells by canonical when equivalences are supplied.
- G2: `attribute_faults(..., equivalences=None)` parameter; loop passes the table.
- G3: A model healthy on any provider it serves is `PROVIDER_FAULT` on a failing
  provider, never `MODEL_FAULT`.
- G4: `MODEL_FAULT` requires ≥2 distinct providers observed dead — a single
  provider's failure can never trigger a model-wide eviction.
- G5: `_match_model_id` is most-specific-wins (longest matching pattern); the broad
  `deepseek-v4` pattern is removed so `deepseek-v4-flash` ≠ `deepseek-v4-pro`.

## Non-Goals
- NG1: Does NOT change per-cell health (Fix-2) nor the cap (Fix-1) nor cold-start
  (Fix-4).
- NG2: Does NOT alter the emitted fault's provider/model id — only the grouping
  used to decide its scope.

## Assumptions
- A1: The equivalence table's `canonical_for_model_id` resolves the provider id
  variants of a shared model to one canonical (verified for the deepseek/glm/qwen
  families that triggered the incident).

## Interface
- `attribution._model_key`, `attribution.attribute_faults(..., equivalences=...)`.
- `chain_loop.plan_and_apply` passes its `equivalences` into `attribute_faults`.

## Behavior
- Same canonical, healthy on provider A + dead on provider B → B is
  `PROVIDER_FAULT` (LOCAL); A is `HEALTHY`.
- Dead on ≥2 distinct providers (canonically), none healthy → `MODEL_FAULT`.
- Dead on a single observed provider → `PROVIDER_FAULT` (drop the ref), never
  `MODEL_FAULT`.
- `deepseek-v4-flash` and `deepseek-v4-pro` resolve to distinct canonicals.
- `equivalences=None` → raw-id grouping (unchanged) under the same guard.

## Architecture Impact
- Amends `attribution.py` (SP-CHAINPILOT-ATTRIBUTION) — new import edge to
  `benchmark_equivalences`, declared in that spec's `depends_on`
  (SP-CHAINPILOT-EQUIVALENCES). Amends the `attribute_faults` call in
  `chain_loop.py` (SP-CHAINPILOT-LOOP). `arch check` green.

## Acceptance Criteria
- [ ] AC1: a model dead on ≥2 providers under *different* id strings is
  `MODEL_FAULT` only WITH equivalences (canonical join); WITHOUT them each is a
  single-provider `PROVIDER_FAULT` — proving the grouping is the cause.
- [ ] AC2: a single-provider failure is `PROVIDER_FAULT` (drop), never
  `MODEL_FAULT`; a model healthy on another provider stays `PROVIDER_FAULT`.
- [ ] AC3: `deepseek-v4-flash` and `deepseek-v4-pro` no longer share a canonical;
  no two distinct live chains ids collide except the intended `mistral-medium-3.5`.
- [ ] AC4: `chain_loop` passes equivalences into `attribute_faults`.
- [ ] AC5: ruff + format + no-inline + mypy(attribution) + `arch check` +
  full `pytest tests/unit` green.

## Open Questions
- Some live ids remain uncovered by the table (`mistral-large-*`, `qwen3.5-*`,
  the OpenRouter `mistral-small-2603`); the `≥2 providers` guard makes that SAFE
  (they get `PROVIDER_FAULT`, never an over-eviction). Tightening the table to
  also enable correct *model-wide* eviction for genuinely-dead uncovered models is
  a later refinement that needs live provider-catalog verification.
