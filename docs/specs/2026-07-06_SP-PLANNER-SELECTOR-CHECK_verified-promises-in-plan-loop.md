---
id: SP-PLANNER-SELECTOR-CHECK
title: Mechanical selector verification in the Planner's refine loop
version: 0.2
status: approved
author: jfaye + Claude (three plan reviews on SP-PLAN-CONTRACT-LINTS-2, 2026-07-05)
created: 2026-07-06
updated: 2026-07-06

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Mechanical selector verification in the Planner's refine loop

## Intent

Move the promised-selector sanity checks the operator performed by
hand — three review rounds on one spec — into the Planner session's
own validate-and-refine loop. Round 2 of SP-PLAN-CONTRACT-LINTS-2 was
rejected because all ten "representative existing" node ids were
hallucinated; round 3 shipped two class-scoped methods promised
without their class segment. Both defect classes are mechanically
detectable at plan time.

## Context

`_parse_and_validate` (`src/ferova/review/planner.py`, frontier) runs
the full `ActionPlan` contract and feeds any rejection back through
`_refine_prompt` (SP-PLANNER-PLAN-RETRY, 3 attempts). It never looks
at the TREE: a selector naming an existing file but a nonexistent
test sails through and kills the session much later at self-verify.
`selector_present` (`src/ferova/review/spec_gate.py`, class-scope
aware since #21) is the existing resolver to reuse. A promised
selector in an existing file is legitimate in exactly two cases: the
node id resolves at head, or the step CREATES it — and the second is
checkable too, because a plan that creates a test must name it in the
promising step's `action` text.

Suggested plan shape (two planning sessions burned their full retry
budgets on coupling errors this decomposition avoids):

- Step 1 — files `[src/ferova/review/planner.py,
  tests/unit/test_review_planner.py]` (the test file already EXISTS:
  it must sit in this step's files for the coupling validator even
  though the step only appends to it), promising exactly the four AC
  node ids, all NEW module-level tests this step adds to that file.
- Step 2 — files `[tests/integration/test_planner_selector_check.py]`
  (NEW file this step creates), promising one node id inside it; the
  plan-level `integration_tests` promises that same selector, and the
  file lives in this step's files so the deliverable validator is
  satisfied. Non-docs step: it must also promise at least one unit
  test — promise the same new integration node id in `unit_tests`
  (integration-tree paths are legal there; only the plan-level list
  is restricted to tests/integration/).

## Goals

- G1: After `ActionPlan` validation succeeds, the Planner session
  additionally resolves every promised selector (step `unit_tests`
  and plan `integration_tests`) whose file exists at head: the
  selector must satisfy `selector_present` OR its node id must appear
  verbatim in the promising step's `action` text (declared creation).
  Violations are rejected like any validation error, with a directive
  message listing each offending selector and the two ways to fix it.
- G2: The check runs INSIDE the Planner's parse-retry loop only —
  `load_plan` / develop-time re-validation are untouched (a committed
  plan's promises were already verified at generation).
- G3: Selectors whose file does not exist at head are exempt (the
  file itself is the step's deliverable; the existing coupling
  validator already forces it into the contract).

## Non-Goals

- NG1: No red-before/green-after execution check (the Planner's tools
  are read-only; discrimination remains operator review and a later
  slice).
- NG2: No plan schema change (no new/existing tagging field — the
  action text is the declaration channel).
- NG3: No change to `ActionPlan`/`PlanStep` validators.

## Assumptions

- A1: `src/ferova/review/planner.py` is unowned in the arch registry
  (frontier), as is `spec_gate.py`'s owner relationship to it; this
  spec owns nothing and the import of `selector_present` into
  `planner.py` resolves inside the review package.
- A2: Requiring the verbatim node id in the action text for created
  tests matches the house AC convention (suggested names are the
  mechanical contract) and the v0.3 plan-shape guidance.

## Interface

Inputs: N/A (internal check in the Planner session).

Outputs: N/A.

Errors:
- The check's rejection text flows through the existing
  `(None, reason)` return of `_parse_and_validate` into
  `_refine_prompt` — no new exception surface.

## Behavior

### Nominal

A plan promising one existing (resolved) selector and one new test
named verbatim in its step's action passes; the session proceeds to
write the plan document.

### Edge cases

- Class-scoped selector whose class segment is wrong → does not
  resolve → rejected with the directive message (the round-3 defect).
- Selector file absent at head → exempt (G3).
- Node id present in the action text but never delivered → out of
  scope here; the step gate and SP-DEV-PROMISE-DELIVERY handle
  delivery.

### Failure scenarios

- Three refine attempts still violating → the session fails loudly
  with the last selector report, nothing written (existing retry
  semantics).

## Architecture Impact

- No edge added or removed; no ownership change.

## Diagram

N/A (one post-validation check in the Planner loop).

## Acceptance Criteria

- [ ] AC1: `tests/unit/test_review_planner.py::test_hallucinated_selector_in_existing_file_is_refined`
  — a candidate plan promising a nonexistent node id in an existing
  file is rejected and the refine prompt carries the selector and
  both remedies.
- [ ] AC2: `tests/unit/test_review_planner.py::test_resolved_selector_is_accepted`
  — a promise that `selector_present` resolves passes the check.
- [ ] AC3: `tests/unit/test_review_planner.py::test_declared_creation_in_action_text_is_accepted`
  — a nonexistent node id whose name appears verbatim in the step's
  action text passes.
- [ ] AC4: `tests/unit/test_review_planner.py::test_selector_in_new_file_is_exempt`
  — a selector whose file does not exist at head is not checked.
- [ ] AC5: The full unit suite passes.

## Open Questions

(none)
