---
id: SP-DEV-STEP-PREFLIGHT
title: Mechanical step preflight — never pay the LLM for a completed step
version: 0.1
status: approved
author: jfaye (improvement-axes report, rank 1)
created: 2026-07-03
updated: 2026-07-03

owns:
  code: [src/repoach/review/dev_runner.py]
  resources: []

depends_on: [SP-ARCH-REVIEW-WIRE, SP-DEV-STEP-LOOP-HARDEN, SP-DEVAGENT-DECOMPOSE,
  SP-DEVAGENT-LOOP, SP-DEVAGENT-SELFVERIFY, SP-DEVAGENT-TOOLS, SP-DEVAGENT-WIRE,
  SP-PLAN-CONTRACT-LINTS]
provides_to: []

constraints: {}
---

# Mechanical step preflight — never pay the LLM for a completed step

## Intent

Before dispatching the Developer on a plan step, verify mechanically
whether the step is already complete — and when it is, mark it done
for zero LLM tokens. Measured waste this addresses: on
SP-FINDINGS-BRIDGE-DOCFIX, two re-dispatches of an already-complete
step concluded "no edits are needed" after burning 396,828 tokens —
83.5% of the spec's entire Developer budget; on SP-ORCH-DOCSTRING,
attempt 5 paid two dispatches (~86k tokens) for a step whose
deliverable was already committed.

## Context

`run_developer_session` (`dev_runner.py`) iterates `action_plan.steps`
and calls `execute_plan_step` per step. Two mechanisms already exist
and are reused, not replaced: `_step_already_committed` (skips a step
whose exact commit subject sits on the session branch —
`origin/develop..HEAD`) and `run_promised_tests(repo_root, selectors)`
(`dev_runner.py:569`, the SP-DEV-PROMISE-RECONCILE gate runner, whose
returns are `(ok, tail, reconciled)`). The gap: a step whose WORK
exists (files present, promised tests green) but whose own commit
subject is absent — e.g. absorbed into an earlier step's commit, or
left uncommitted by a prior failed attempt — is still fully
re-dispatched, and the honest Developer who writes nothing then fails
the step ("loop ended without writing any file").

## Goals

- G1: A pure predicate `step_preflight_complete(repo_root, plan, step)
  -> bool` returns True exactly when: every path in `step.files`
  exists at `repo_root`, AND `run_promised_tests` passes for the
  step's promised selectors — defined as `step.unit_tests` PLUS every
  `plan.integration_tests` selector whose file path is in
  `step.files`. An empty selector set → False (nothing mechanical to
  prove completion; dispatch normally).
- G2: `run_developer_session` consults the predicate after the
  commit-subject fast path and before dispatching: a preflight-complete
  step increments `steps_completed`, logs
  `dev_runner.step_preflight_complete`, records a zero-token audit row
  via `record_coder_response` (pr_number=0, model_used="preflight",
  tokens_used=0, summary naming the step and the green selectors), and
  is never dispatched to the Developer.
- G3: The uncommitted-work case closes the loop: when preflight passes
  but the step's changes are uncommitted on disk, the session commits
  them with the step's `commit_message` (via the existing
  `commit_paths` on the step-contract files that differ from HEAD)
  before counting the step complete.

## Non-Goals

- NG1: No execution of `done_when` prose (shell-executing arbitrary
  done_when strings is a separate, later slice).
- NG2: No change to `execute_plan_step`'s internal gates, attempts, or
  the contract-escape check.
- NG3: No change to plan schemas or the Planner.

## Assumptions

- A1: `run_promised_tests` is side-effect-free on green (it only runs
  pytest) and safe to call before any dispatch.
- A2: `dev_runner.py` is unowned in the arch registry (verified
  2026-07-03), so this spec may claim it without a disjointness
  conflict; the test file needs no owner (ownership governs
  boundaries, not working sets).

## Interface

Inputs:
- `repo_root`: Path — the session worktree root.
- `plan`: ActionPlan — for `integration_tests` attribution.
- `step`: PlanStep — files, unit_tests, commit_message.

Outputs:
- `step_preflight_complete(...) -> bool` — pure, no writes.

Errors:
- None raised: any pytest/git error inside the predicate returns
  False (fail-open to a normal dispatch, never to a false skip).

## Behavior

### Nominal

Session start on a resumed branch: step 1's subject is on the branch
(fast path skips it); step 2's files exist and its promised selectors
are green → preflight completes it for zero tokens; step 3 is genuinely
todo → dispatched normally.

### Edge cases

- A step with promised selectors green but a contract file missing →
  False (dispatch).
- A step whose only promised selectors are inherited plan-level
  integration tests not living in its files → those selectors are NOT
  attributed to it; with no attributed selectors → False (dispatch).
- Preflight-green work sitting uncommitted → committed with the step's
  message, then counted complete (G3).

### Failure scenarios

- pytest crashes or times out inside the predicate → False, log
  `dev_runner.step_preflight_error`, dispatch normally.

## Architecture Impact

- No edge added or removed. Both owned files move from the frontier
  into this spec's `owns.code`.

## Diagram

N/A (single-module control-flow addition).

## Acceptance Criteria

- [ ] AC1: `tests/unit/test_review_plan_executor.py::test_preflight_completes_a_green_step_for_zero_tokens`
  — seeds a repo where the step's files exist and promised tests pass,
  runs the session loop, asserts `steps_completed` incremented, the
  Developer fake was never called for that step, and a
  `pr_coder_responses` row with model_used="preflight" and
  tokens_used=0 exists.
- [ ] AC2: `tests/unit/test_review_plan_executor.py::test_preflight_dispatches_when_a_contract_file_is_missing`
  — promised tests green but one `step.files` path absent → the
  Developer fake IS called.
- [ ] AC3: `tests/unit/test_review_plan_executor.py::test_preflight_commits_uncommitted_green_work`
  — green work on disk, uncommitted → the step's commit subject
  appears in the branch log and the Developer fake is never called.
- [ ] AC4: `tests/unit/test_review_plan_executor.py::test_preflight_attributes_integration_selectors_by_file`
  — a plan-level integration selector living in the step's files is
  required by the predicate (red integration test → dispatch).
- [ ] AC5: The full unit suite passes.

## Open Questions

(none)
