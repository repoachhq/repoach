---
id: SP-PROMISE-RENAME-RETIRE
title: Retire mechanical promise laundering — an unrelated green test cannot satisfy a promise
version: 0.1
status: draft
author: jfaye (Ferova audit 2026-07-13)
created: 2026-07-13
updated: 2026-07-13

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Retire mechanical promise laundering — an unrelated green test cannot satisfy a promise

## Intent

The promised-test contract exists so a plan step is only accepted when
the test it promised actually exists. The mechanical-rename fallback
defeats that: it renames whatever unrelated green test the loop
happened to write into the plan's promised name and re-runs, laundering
an unimplemented promise into a green selector that every downstream
gate then trusts. Retire the laundering.

## Context

Audit 2026-07-13 finding M2. `src/ferova/review/dev_runner.py`:

- `_attempt_mechanical_rename` (lines 117-179): when exactly one
  promised node id is missing and exactly one unpromised test function
  is present, it rewrites `def <delivered>(` to `def <promised>(` in
  place (line 163) and returns `("renamed", original)`.
- Applied in the step gate at lines 1497-1551: after a `reconciled`
  green run that touched the promised file(s), it computes
  `delivered_names` (line 1506), calls `_attempt_mechanical_rename`
  (1507-1509), and on `renamed` re-runs the promised selectors
  (line 1530); if green it logs `promised_tests_renamed` (1532-1537)
  and accepts the step.
- Downstream, `selector_present` (self-verify, SP-SELFVERIFY), spec
  coverage (`spec_gate.compute_spec_coverage`) and the merge gate's
  `_MECHANICAL_TYPES` re-verification all treat the now-present
  `def test_<promised>(` as evidence the promise was kept.

Any green test — regardless of what it asserts — can be laundered into
the promised name because the rename matches on function-name shape
only, never on what the test covers. This runs inside `execute_plan_step`
before the branch reaches review. Review-integrity change, not a
merge-path change.

## Goals

- G1: a step whose promised selector is absent must FAIL its promise
  gate (retryable feedback to the loop), never pass by renaming an
  unrelated test into the promised name.
- G2: remove the mechanical-rename path (or constrain it so it can
  provably never make an unrelated test satisfy a promise) together
  with its now-dead restore/re-run scaffolding, keeping the module free
  of unreachable code.
- G3: the loop still receives clear, actionable feedback naming the
  exact promised selectors it must write.

## Non-Goals

- NG1: no change to the promised-test PRESENCE check itself
  (`promised_present`, `_promised_test_files`) — only the rename
  laundering is removed.
- NG2: no change to the legitimate "reconciled but promised file
  untouched" feedback branch (lines 1482-1496).
- NG3: no attempt to semantically judge test bodies in this spec — the
  fix is to stop laundering, not to add a behavioral judge.

## Assumptions

- A1: retiring the rename is the sanctioned resolution (the finding
  offers "retire the mechanical rename" as the primary fix); the
  Developer loop is capable of writing a correctly-named test when the
  gate tells it the exact selector, so no capability regression.
- A2: `_attempt_mechanical_rename`, `_restore_file_contents` and the
  rename re-run block become dead once the call site is removed and are
  deleted in the same change to satisfy the no-dead-code review lens.

## Interface

N/A (in-place fix, no public signature change). Private helpers
`_attempt_mechanical_rename` / `_restore_file_contents` are removed;
their unit coverage is retargeted to the fail path.

## Behavior

### Nominal

A step whose promised selectors are all present after a green run is
accepted, exactly as today (no rename was needed).

### Edge cases

- Reconciled green, promised file touched, but a promised selector is
  ABSENT and an unrelated test function is present: the step FAILS the
  promise gate with `gate_feedback` naming the exact promised selectors
  (the current `absent_promises` branch at lines 1552-1568 becomes the
  sole outcome for this case). No rename attempted.
- Multiple missing promises or multiple unpromised tests (previously
  `"ambiguous"`): identical fail-the-gate outcome — no special-casing
  survives.

### Failure scenarios

- The loop keeps writing a wrongly-named test across retries → the step
  exhausts its retry budget and fails loudly with the promised
  selectors named, never a false accept. Fail closed on the promise
  contract.

## Architecture Impact

- Adds/Removes dependency: none — in-place modification of
  `dev_runner.py` (owned by an existing spec, SP-DEV / the dev-runner
  arc). Removes internal helpers only; no cross-owner import change.
- New / changed coupling, cycles, or shared state: removes the coupling
  between delivered test names and promised names introduced by the
  rename.

## Diagram

N/A (in-place fix)

## Acceptance Criteria

- [ ] AC1: unit — the step gate, given a touched promised file where
  the promised selector is absent but an unrelated `def test_*` is
  present, produces a fail outcome whose feedback names the promised
  selectors; no source file on disk is rewritten to the promised name.
- [ ] AC2 (INTEGRATION): drive `execute_plan_step` (or the smallest
  real `dev_runner` entrypoint that runs the promise gate) against a
  tmp git repo where the loop is a truthful boundary fake that writes
  one unrelated green test into the promised file. The step FAILS the
  promise gate and the promised file on disk still lacks the promised
  `def <promised>(` definition — confirming no laundering occurred.
- [ ] AC3: promised test file + selectors —
  `tests/unit/test_dev_runner_promise_delivery.py::test_unrelated_green_test_fails_promise_not_renamed`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `ferova arch graph --check` exits 0; no dead/unreachable code left
  behind by the removal.

## Open Questions

(none)
