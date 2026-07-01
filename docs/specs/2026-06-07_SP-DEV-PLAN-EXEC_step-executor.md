# SP-DEV-PLAN-EXEC — plan-driven step executor (builder slice)

## Metadata

- **Status**: OPEN
- **Priority**: P1
- **Owner**: operator
- **Executor**: hand-implemented (core runner rework)
- **Opened**: 2026-06-07

## Why

Architecture decisions #2/#3/#4: today `run_developer_session` is one
monolithic 32K-token dispatch — the whole implementation in a single
model response, a single commit, with the 500-LOC cliff and the
escaping-error lottery that killed the SP-PLAN-FORM dispatches. The
target design executes the Planner's validated `ActionPlan` step by
step: small per-step outputs, one commit per step, gates per step.

## What

Rework `src/ferova/review/dev_runner.py` around the plan:

1. **Plan first**: `run_developer_session(spec_id)` loads the plan
   via `load_plan` (from `docs/plans/<SP-ID>.md`); when absent, it
   calls `run_planner_session` to produce it. The rendered plan
   document is committed as the FIRST commit of the implementation
   branch (`docs(plan): <SP-ID> action plan`).
2. **Per-step execution loop**: for each `PlanStep` in order, build a
   step-scoped prompt for the Developer (the step's `action` +
   `files` contents + the plan summary for context — NOT the whole
   spec), apply the returned fixes through the existing path
   whitelist, then run the step gates: python syntax check, `ruff
   check`, and the step's promised `unit_tests` via pytest. Green →
   commit with the step's `commit_message`. Red → revert the step's
   changes and retry once with the gate output appended to the
   prompt; red again → stop the session loudly (partial branch
   stays, no silent abandon).
3. **Session wrap-up**: after the last step, run the full unit suite
   plus the plan's `integration_tests` when present; on green, push
   the branch and open the PR exactly like today.
4. **Outcome**: extend `DevSessionResult` with `steps_completed`,
   `steps_total`, `failed_step_index`, `plan_committed`.

## Files in scope

- `src/ferova/review/dev_runner.py`
- `src/ferova/review/planner.py`
- `src/ferova/review/plan.py`
- `src/ferova/review/reviewer.py`
- `tests/unit/test_review_dev_runner.py`
- `tests/unit/test_review_plan_executor.py` (new)

## Out of scope

- The Change-Request Analyst input path (REVIEW loop slice).
- `tests/integration/` CI stage creation (SP-INTEGRATION-STAGE).
- Any prompt changes under `prompts/review/` beyond what the step
  prompt assembly needs from code.

## Smoke scenario

### Setup

Proxy on :8082, a committed plan for a small test spec.

### Execute

```bash
ferova develop SP-DEMO-SLICE
```

### Expected

Branch carries: first commit = the plan document, then one commit per
completed step with the plan's commit messages; PR opened on green.

## Definition of Done

- Plan-first flow (load or produce, commit as first commit).
- Step loop with per-step gates, one commit per green step, one
  retry per red step, loud stop on the second red.
- Full-suite + integration wrap-up before push.
- `DevSessionResult` extended; all paths unit-tested with a fake
  Developer (no live model in tests).
- `ruff` + full `pytest tests/unit` green.

## Commit plan

1. `feat(review): plan-first session bootstrap — load/produce plan, commit it`
2. `feat(review): per-step executor with gates, commits and one retry`
3. `feat(review): session wrap-up — full suite, integration tests, push + PR`
4. `test(review): plan executor suite with fake Developer`

## Risks

- Step prompts must carry enough context without re-flooding the
  window — the plan's `files` list is the contract; a step naming
  too few files fails its gate and surfaces the planning gap.
- Reverting a red step must not touch earlier green commits (use
  `git checkout -- <step files>` + `git clean` scoped to the step's
  new files, never a branch-wide hard reset).
