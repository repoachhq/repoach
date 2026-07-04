---
id: SP-DEV-STEP-PREFLIGHT-1
title: "Mechanical step preflight \u2014 predicate, session integration, and uncommitted-work commit"
version: 0.1
status: draft
author: agent

owns:
  code: []
  resources: []

depends_on: [SP-ARCH-GRAPH, SP-DEVAGENT-LOOP, SP-PLAN-CONTRACT-LINTS]
provides_to: []
constraints: {}
---

## Goals
- G1: Add a pure predicate `step_preflight_complete(repo_root, plan, step) -> bool` to `dev_runner.py` that returns True exactly when: every path in `step.files` exists at `repo_root`, AND `run_promised_tests` passes for the step's promised selectors — defined as `step.unit_tests` PLUS every `plan.integration_tests` selector whose file path is in `step.files`. An empty attributed selector set → False (nothing mechanical to prove completion; dispatch normally).
- G2: Modify `run_developer_session` so that after the commit-subject fast path (`_step_already_committed`) and before dispatching to the Developer, it consults `step_preflight_complete`. A preflight-complete step increments `steps_completed`, logs `dev_runner.step_preflight_complete`, records a zero-token audit row via `record_coder_response` (pr_number=0, model_used="preflight", tokens_used=0, summary naming the step and the green selectors), and is never dispatched to the Developer.
- G3: Close the uncommitted-work loop: when preflight passes but the step's changes are uncommitted on disk, the session commits them with the step's `commit_message` via the existing `commit_paths` on the step-contract files that differ from HEAD, before counting the step complete.

## Behavior
### Nominal
Session start on a resumed branch: step 1's commit subject is on the branch (fast path skips it); step 2's files exist and its promised selectors are green → preflight completes it for zero tokens; step 3 is genuinely todo → dispatched normally.

### Predicate logic (G1)
- Build the attributed selector set: start with `step.unit_tests`; then for each selector in `plan.integration_tests`, check whether the selector's file path (the test file the selector refers to) is in `step.files`; if so, include it.
- If the attributed selector set is empty → return False.
- If any path in `step.files` does not exist at `repo_root` → return False.
- Call `run_promised_tests(repo_root, attributed_selectors)`; return True only when it reports ok.
- Any pytest or git error inside the predicate → return False, log `dev_runner.step_preflight_error`, fail-open to a normal dispatch.

### Session integration (G2)
- Insert the preflight check in `run_developer_session`'s per-step loop, positioned after `_step_already_committed` and before `execute_plan_step`.
- On True: increment `steps_completed`, emit the `dev_runner.step_preflight_complete` log line, call `record_coder_response` with the zero-token audit row, and `continue` to the next step without dispatching.
- On False: proceed to dispatch as before.

### Uncommitted-work commit (G3)
- When preflight returns True, check whether the step's files differ from HEAD (i.e., there are uncommitted changes among `step.files`).
- If they differ, call `commit_paths` on the differing files with `step.commit_message` so the step's commit subject appears in the branch log.
- Then proceed with the G2 accounting (increment, log, audit row, skip dispatch).

### Edge cases
- A step with promised selectors green but a contract file missing → False (dispatch).
- A step whose only promised selectors are inherited plan-level integration tests not living in its files → those selectors are NOT attributed to it; with no attributed selectors → False (dispatch).
- Preflight-green work sitting uncommitted → committed with the step's message, then counted complete.
- pytest crashes or times out inside the predicate → False, log `dev_runner.step_preflight_error`, dispatch normally.

## Acceptance Criteria
- [ ] AC1: `tests/unit/test_review_plan_executor.py::test_preflight_completes_a_green_step_for_zero_tokens` — seeds a repo where the step's files exist and promised tests pass, runs the session loop, asserts `steps_completed` incremented, the Developer fake was never called for that step, and a `pr_coder_responses` row with model_used="preflight" and tokens_used=0 exists.
- [ ] AC2: `tests/unit/test_review_plan_executor.py::test_preflight_dispatches_when_a_contract_file_is_missing` — promised tests green but one `step.files` path absent → the Developer fake IS called.
- [ ] AC3: `tests/unit/test_review_plan_executor.py::test_preflight_commits_uncommitted_green_work` — green work on disk, uncommitted → the step's commit subject appears in the branch log and the Developer fake is never called.
- [ ] AC4: `tests/unit/test_review_plan_executor.py::test_preflight_attributes_integration_selectors_by_file` — a plan-level integration selector living in the step's files is required by the predicate (red integration test → dispatch).
- [ ] AC5: The full unit suite passes.
