---
id: SP-PLAN-CONTRACT-LINTS-2
title: Discriminating promises — node ids required, integration under tests/integration
version: 0.3
status: approved
author: jfaye (SP-BUDGET-RETRY-FIXES post-mortem, 2026-07-05)
created: 2026-07-05
updated: 2026-07-05

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Discriminating promises — node ids required, integration under tests/integration

## Intent

Make every plan promise discriminate the step's NEW work. The
generated SP-BUDGET-RETRY-FIXES plan promised bare test files for its
two code steps: pre-existing green tests satisfied the preflight
predicate vacuously and the session ended "3/3 complete" with zero
commits on the branch. The same plan promised its "integration" test
as a unit path, which later masked missing node ids inside a mixed
pytest invocation.

## Context

`ActionPlan` / `PlanStep` (`src/ferova/review/plan.py`, owned by
SP-PLAN-CONTRACT-LINTS — this spec modifies without claiming) already
validate selector safety, per-step unit promises, the src-touching
integration interlock and the deliverable-promise coupling. Two gaps
remain, both exploited by real sessions on 2026-07-05:

- A step's `unit_tests` may be a bare file path. A bare path proves
  nothing about new work: any pre-existing green test in the file
  makes `step_preflight_complete` and the step gate vacuously green.
- `integration_tests` selectors may point anywhere. A unit-tree path
  satisfies the src-interlock to the letter while defeating its
  intent, and mixing it into an attributed selector set produced the
  pytest masking bug that #34 had to guard against.

The error messages reach the Planner in its validate-and-refine loop
(`_parse_and_validate` / `_refine_prompt`), so directive text is a
self-repair instruction. The in-repo test fixtures that construct
plans with bare unit promises or unit-path integration promises must
migrate in the same change — the Planner should enumerate offenders
with `grep -rn "unit_tests=\|integration_tests=" tests/` and include
every affected fixture file in a step's contract so the session's own
gates can reach them.

Plan-shape requirements (a first plan for this spec was rejected in
operator review for violating all three):

- MIGRATION STEPS PROMISE NODE IDS TOO. Once the validators land, an
  unmigrated fixture file fails wholesale at plan construction, so any
  ONE representative existing `::node id` per migrated file is a
  discriminating promise — use that, never a bare file path (a bare
  path is exactly the shape this spec outlaws, and it lets a partial
  migration pass vacuously).
- EVERY STEP COMMITS A DIFF. No verification-only step ("re-read and
  run the suite" produces nothing to commit and fails the runner's
  commit gate); AC5's full-suite verification is the session's own
  wrap-up gate, not a plan step.
- FIXTURE PROMISES LIVE INSIDE THE FIXTURE'S TMP REPO. Migrating a
  fixture's `integration_tests` to `tests/integration/...` means
  updating the paths the fixture seeds and constructs in its OWN tmp
  repository — never creating real `tests/integration/` files for
  fixture use, which would sit outside the step's contract.
- REPRESENTATIVE SELECTORS MUST BE VERIFIED, NOT INVENTED. A second
  plan was rejected because all ten "representative existing" node
  ids were hallucinated — none existed, which kills the session at
  self-verify. Before promising a selector in an EXISTING file, run
  `grep_repo` with pattern `def <test_name>` and confirm exactly the
  promised name; quote the matching `path:line` in the step's action
  text as proof. A selector you did not verify this way must not
  appear in the plan.

## Goals

- G1: A new `PlanStep` validator rejects any `unit_tests` selector
  without a `::` node id, with a directive message ("promise the
  exact test function: file.py::test_name — a bare file proves
  nothing about this step's new work").
- G2: A new `ActionPlan` validator rejects any `integration_tests`
  selector whose file part does not start with `tests/integration/`,
  with a directive message naming the offending selector.
- G3: Every in-repo fixture constructing plans (unit and integration
  test trees) satisfies the two rules; the full suite passes.

## Non-Goals

- NG1: No change to `run_promised_tests`, the preflight predicate, or
  any gate — the lints act at plan validation only.
- NG2: No re-validation of historical `docs/plans/*.md` documents
  (same posture as SP-PLAN-CONTRACT-LINTS: plans of shipped specs are
  archives, re-loaded only if their spec is re-run).
- NG3: No requirement that promised node ids be ABSENT before the
  step runs (a resumed branch legitimately carries them).

## Assumptions

- A1: `src/ferova/review/plan.py` stays owned by
  SP-PLAN-CONTRACT-LINTS; this spec owns nothing (modification ≠
  ownership) and adds no import edge.
- A2: The historical plans that violate the new rules
  (SP-BUDGET-RETRY-FIXES among them) are never re-loaded; the
  existing precedent covers them.

## Interface

Inputs: N/A (validators on existing models).

Outputs: N/A.

Errors:
- `pydantic.ValidationError` at plan construction with the directive
  messages of G1/G2.

## Behavior

### Nominal

A plan promising `tests/unit/test_x.py::test_new_thing` per step and
`tests/integration/test_x_flow.py::test_e2e` at plan level validates
exactly as before.

### Edge cases

- A docs-only step with `unit_tests: []` stays valid (the existing
  docs exemption is untouched).
- `integration_tests: []` on a docs-only plan stays valid.
- A selector with multiple `::` segments (class-scoped) satisfies G1.

### Failure scenarios

- Violating plans inside the Planner session → `planner.plan_invalid`
  plus one refinement turn carrying the directive message (existing
  retry loop, unchanged).

## Architecture Impact

- No edge added or removed; no ownership change.

## Diagram

N/A (two validators + fixture migration).

## Acceptance Criteria

- [ ] AC1: `tests/unit/test_review_plan.py::test_bare_file_unit_promise_is_rejected`
  — a step promising `tests/unit/test_x.py` (no node id) raises
  `ValidationError` whose message contains "promise the exact test
  function".
- [ ] AC2: `tests/unit/test_review_plan.py::test_node_id_unit_promise_is_accepted`
  — the same step with `::test_name` validates.
- [ ] AC3: `tests/unit/test_review_plan.py::test_unit_path_integration_promise_is_rejected`
  — `integration_tests: ["tests/unit/test_x.py"]` raises
  `ValidationError` naming the selector.
- [ ] AC4: `tests/unit/test_review_plan.py::test_integration_tree_promise_is_accepted`
  — `integration_tests: ["tests/integration/test_x.py::test_e2e"]`
  validates.
- [ ] AC5: The full unit suite passes (all in-repo fixtures migrated).

## Open Questions

(none)
