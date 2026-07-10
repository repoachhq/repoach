---
id: SP-PLAN-STEP-DENSITY-CAP
title: Cap plan-step action density (chars per file) at Planner emission
version: 0.1
status: approved
author: jfaye (two 30-turn-budget blowouts in batch 2; empirical grounding workflow 2026-07-09; operator GO)
created: 2026-07-09
updated: 2026-07-09

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Cap plan-step action density (chars per file) at Planner emission

## Intent

Stop the "huge action text, few files" plan step that passes every
existing cap yet blows the autonomous Developer's 30-turn tool budget.
Add ONE strict-layer check at Planner emission — an action-DENSITY cap
(chars per file) — registered in the rule catalog so the Planner sees
it from the first prompt. Never retroactive; committed plans are
grandfathered exactly as SP-PLAN-QUALITY (#66) established.

## Context

SP-PLAN-QUALITY (#66) shipped `validate_plan_form_strict(plan)`
(`src/ferova/review/plan.py:107`) with `PLAN_STEP_MAX_FILES = 3` and
`PLAN_STEP_MAX_UNIT_SELECTORS = 5`, enforced only in the Planner
emission/refine loop (`planner.py:440` proxy, `:533` claude_cli),
never in `load_plan`. Two batch-2 steps still blew the 30-turn budget.

An empirical grounding workflow (2026-07-09, 109 committed plan steps
measured) reshaped the fix:

- **Raw char count does not discriminate.** The dead-hop known-bad
  step was 2278 chars — SMALLER than 10 committed steps that succeeded
  (up to 4842). No single char threshold is both above every success
  and below both known-bad cases; the naive "action-length cap" target
  is infeasible.
- **The two blowups split by mechanism.** Dead-hop's bad step had
  **4 files** — already rejected by the existing `PLAN_STEP_MAX_FILES=3`
  cap (it only shipped because it was hand-authored, bypassing the
  emission gate). Wrapup's bad step was **5664 chars in only 2 files**
  (density 2832 chars/file) — it sails past the file cap. That
  few-files/huge-action pathology is the sole gap a new cap must close.
- **Density separates cleanly.** Action density = chars / files.
  Wrapup known-bad = 2832/file; the densest committed SUCCESS ≈ 2421/
  file. Unlike raw chars, density is normalized — it does not punish a
  legitimately multi-file step. A cap of **2600 chars/file** admits
  every committed step and flags the wrapup pathology, ~7-8% margin
  each side.

## Goals

- G1: A new per-step check in `validate_plan_form_strict`: a step whose
  `len(step.action) / max(1, len(step.files))` exceeds
  `PLAN_STEP_MAX_ACTION_DENSITY` (new module constant, `= 2600`) is
  refused. The reason string cites the measured density, the cap, and
  the literal `30-turn` budget rationale — parity with the existing
  size-cap reasons so SP-PLAN-QUALITY's convergence telemetry treats
  it identically.
- G2: The rule sentence is registered in `_STRICT_FORM_RULES`
  (`plan.py:60-71`) so `render_plan_form_rules()` surfaces it in the
  INITIAL Planner catalog (both backends) — the model sees it before
  any failure, never discovering it one-rejection-at-a-time.
- G3: Emission-only. `load_plan` stays permissive and no pydantic
  validator gains a density check; committed plans are grandfathered
  (a retroactive rule would brick shipped plans, same as #66's OQ1).

## Non-Goals

- NG1: No raw action-char cap (empirically a non-discriminating
  signal — see Context).
- NG2: No change to the file or unit-selector caps; dead-hop's
  pathology is already owned by `PLAN_STEP_MAX_FILES`.
- NG3: No edits under `prompts/review/` (catalog rides the code-side
  prompt assembly).

## Assumptions

- A1: `PlanStep.action` is a non-empty `str` and `PlanStep.files` a
  `list[str]`; `len` on both is trivially available (both already read
  in `validate_plan_form_strict`). `max(1, len(files))` guards the
  zero-file edge.
- A2: The densest committed successful step is ≈2421 chars/file; a
  2600 cap rejects zero committed steps (verified in the grounding
  workflow).

## Behavior

At Planner emission, after a payload parses to a valid `ActionPlan`,
`validate_plan_form_strict` runs; a step over the density cap adds a
reason to the returned list, which folds into the existing refine
feedback (`planner.py:441-443` / `534-536`) — the Planner splits the
step and retries, exactly like the file/selector caps today. Nothing
changes for `load_plan` or committed plans.

## Acceptance Criteria

- AC1: `tests/unit/test_plan_form_rules.py::test_action_density_cap_rejects_dense_step`
  — a step with 2 files and an action longer than `2 *
  PLAN_STEP_MAX_ACTION_DENSITY` chars yields a reason containing the
  cap value AND the literal `30-turn`; the SAME long action spread
  across enough files to drop below the cap yields NO reason (proves
  it is density, not raw length).
- AC2: `tests/unit/test_plan_form_rules.py::test_action_density_rule_in_catalog`
  — the density sentence is a key of `_STRICT_FORM_RULES` and appears,
  uniquely numbered, in `render_plan_form_rules()`.
- AC3: `tests/unit/test_plan_form_rules.py::test_action_density_cap_is_emission_only`
  — a plan whose step exceeds the density cap still loads through
  `load_plan`/`parse_plan_markdown` without raising (grandfathering);
  only `validate_plan_form_strict` flags it.
- AC4: Full unit suite green — existing short-action fixtures
  (~40-80 chars, density tiny) are unaffected; `PLAN_STEP_MAX_ACTION_DENSITY`
  is re-exported in the test import block.

## Open Questions

- OQ1: Should density also inform a Planner PROMPT hint ("keep each
  step's action focused — roughly one deliverable")? Default: the
  catalog sentence (G2) already carries this; no extra prose.
