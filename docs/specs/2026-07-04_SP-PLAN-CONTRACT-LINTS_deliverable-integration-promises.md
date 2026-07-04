---
id: SP-PLAN-CONTRACT-LINTS
title: Deliverable integration promises — plan-form lint
version: 0.1
status: approved
author: jfaye + Claude (improvement-axes report; gap named in commit 509b9e7)
created: 2026-07-04
updated: 2026-07-04

owns:
  code: [src/ferova/review/plan.py]
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Deliverable integration promises — plan-form lint

## Intent

Reject at plan-validation time any plan whose plan-level
`integration_tests` promise names a test file that no step's file
contract creates — the shape that makes a plan unsatisfiable: the
per-step jail forbids every session from ever writing the promised
file, while spec coverage requires it.

## Context

`ActionPlan._promised_tests_are_created_by_the_plan`
(`src/ferova/review/plan.py:194-236`) already enforces exactly this
coupling for per-step `unit_tests` (with index ordering, since a
step's gate runs its own selectors). Plan-level `integration_tests`
selectors have no such rule: they are flag-guarded only. The gap is
not hypothetical — the SP-ORCH-DOCSTRING plan promised
`tests/integration/test_orchestrator_docstring_integration.py` while
no step's `files` included that path; every autonomous session hit
the wall until the operator repaired the plan by hand (commit
509b9e7, whose message names this rule and this spec id).

The new rule's error message reaches the Planner model directly: plan
validation runs inside the Planner session's validate-and-refine loop
(`_parse_and_validate`, `src/ferova/review/planner.py:41-57`, with
`_PLAN_PARSE_ATTEMPTS = 3` and `_refine_prompt` feeding the exact
validator text back), so a directive message is a self-repair
instruction, per the house error-string doctrine.

## Goals

- G1: A new `model_validator(mode="after")` on `ActionPlan` rejects
  the plan when any `integration_tests` selector's file part (the
  selector text before `::`) is absent from the union of ALL steps'
  `files`. Any step may create the file, regardless of index —
  integration tests run at session end, so the ordering constraint of
  the unit-side rule does not apply.
- G2: The error message is directive, mirroring the unit-side rule:
  it names the offending selector, the missing file, and the fix
  ("add that file to a step's files — the per-step jail forbids
  writing files outside every contract").
- G3: All existing rules and behaviours are unchanged; a plan with an
  empty `integration_tests` list remains vacuously valid under this
  rule (the src-touching interlock at `plan.py:186-192` is separate
  and untouched).

## Non-Goals

- NG1: No semantic decomposition linting (the atomic-rename /
  import-breaking step-split deadlock is a distinct axis, not
  mechanically checkable at form level).
- NG2: No conventional-commit format enforcement on `commit_message`.
- NG3: No change to the Planner loop, its retry budget, or its
  prompts — the in-loop feedback channel already exists.
- NG4: No migration or repair of legacy committed plan documents.

## Assumptions

- A1: `src/ferova/review/plan.py` is unowned in the arch registry
  (verified 2026-07-04: `Registry.owner_of` returns `None`), so this
  spec may claim it without a disjointness conflict; the test file
  needs no owner (ownership governs boundaries, not working sets).
- A2: Two legacy plans of already-shipped specs
  (`SP-FINDINGS-BRIDGE-DOCFIX`, `SP-INTEGRATION-STAGE`) would fail
  the new rule if ever re-loaded; they are historical documents of
  completed work and are never re-run. Precedent: the committed
  `SP-DEV-PLAN-EXEC` plan is already invalid under the existing
  unit-side rule today.

## Interface

Inputs: N/A (a validator on the existing `ActionPlan` model; no new
public API).

Outputs: N/A (validation side effect only).

Errors:
- `pydantic.ValidationError` (a `ValueError`): raised at
  `ActionPlan` construction when an integration promise is not
  created by any step, with the directive message of G2.

## Behavior

### Nominal

A plan promising `tests/integration/test_x.py::test_e2e` where some
step (any index) lists `tests/integration/test_x.py` in its `files`
validates exactly as before.

### Edge cases

- Selector with a `::` node id → only the file part before the first
  `::` is checked against step files.
- The creating step has a higher index than other steps → accepted
  (no ordering requirement, unlike the unit-side rule).
- `integration_tests: []` → vacuously valid under this rule.

### Failure scenarios

- A violating plan inside the Planner session → one
  `planner.plan_invalid` log and a refinement turn carrying the
  directive message; after 3 failed attempts the session fails loudly
  with nothing written (existing behaviour).
- A violating committed plan re-loaded at `ferova develop` time →
  the existing loud terminal error ("committed plan is invalid"),
  which is correct: such a plan burns sessions if allowed through.

## Architecture Impact

- No edge added or removed. `src/ferova/review/plan.py` moves from
  the frontier into this spec's `owns.code`; its intra-repo imports
  resolve to frontier files, so `depends_on` stays empty.

## Diagram

N/A (single-validator addition).

## Acceptance Criteria

- [ ] AC1: `tests/unit/test_review_plan.py::test_integration_promise_without_creating_step_is_rejected`
  — a plan whose `integration_tests` names a file absent from every
  step's `files` raises `ValidationError`, and the message contains
  the selector, the missing file path, and the phrase "add that file".
- [ ] AC2: `tests/unit/test_review_plan.py::test_integration_promise_created_by_any_step_is_accepted`
  — the promised integration file appears only in the LAST step's
  `files` (higher index than the plan's other steps) and the plan
  validates.
- [ ] AC3: `tests/unit/test_review_plan.py::test_integration_promise_node_id_resolves_file_part`
  — a selector of the form `path::node` validates when `path` is in a
  step's `files`.
- [ ] AC4: `tests/unit/test_review_plan.py::test_docs_only_plan_with_empty_integration_promises_stays_valid`
  — a docs-only plan with `integration_tests: []` still validates
  (vacuous pass; the src-interlock exemption is preserved).
- [ ] AC5: The full unit suite passes.

## Open Questions

(none)
