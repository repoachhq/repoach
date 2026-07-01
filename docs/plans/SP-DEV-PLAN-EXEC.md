# SP-DEV-PLAN-EXEC — Plan-Driven Step Executor

Rework the Developer session runner to execute action plans step-by-step with per-step validation and commits.

## Step 1 — Load or generate action plan

- **Files**: `src/ferova/review/dev_runner.py`, `src/ferova/review/plan.py`
- **Action**: Modify run_developer_session to first load or generate the action plan, then commit it as the first implementation commit
- **Commit**: `feat(review): plan-first session bootstrap`
- **Done when**: Action plan is loaded/generated and committed as first commit
- **Unit tests**: `tests/unit/test_review_dev_runner.py::test_load_or_generate_plan`

## Step 2 — Implement step execution loop

- **Files**: `src/ferova/review/dev_runner.py`
- **Action**: Add the per-step execution loop with gates (syntax, ruff, unit tests) and one-retry mechanism
- **Commit**: `feat(review): per-step executor with gates`
- **Done when**: Developer can execute plan steps with proper validation and retry logic
- **Unit tests**: `tests/unit/test_review_plan_executor.py::test_execute_plan_step`, `tests/unit/test_review_plan_executor.py::test_execute_plan_with_failure`

## Step 3 — Implement session wrap-up

- **Files**: `src/ferova/review/dev_runner.py`
- **Action**: Add logic to run full test suite and integration tests after all steps complete, then push and open PR
- **Commit**: `feat(review): session wrap-up with full suite and integration tests`
- **Done when**: Full test suite and integration tests run successfully before pushing branch and opening PR
- **Unit tests**: `tests/unit/test_review_dev_runner.py::test_session_wrapup`

## Step 4 — Extend DevSessionResult

- **Files**: `src/ferova/review/dev_runner.py`
- **Action**: Add steps_completed, steps_total, failed_step_index, and plan_committed fields to DevSessionResult
- **Commit**: `feat(review): extend DevSessionResult with execution tracking fields`
- **Done when**: DevSessionResult includes new fields for tracking execution progress
- **Unit tests**: `tests/unit/test_review_dev_runner.py::test_dev_session_result_fields`

## Integration tests

- `tests/integration/test_developer_session.py`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-DEV-PLAN-EXEC",
  "title": "Plan-Driven Step Executor",
  "summary": "Rework the Developer session runner to execute action plans step-by-step with per-step validation and commits.",
  "steps": [
    {
      "index": 1,
      "title": "Load or generate action plan",
      "files": [
        "src/ferova/review/dev_runner.py",
        "src/ferova/review/plan.py"
      ],
      "action": "Modify run_developer_session to first load or generate the action plan, then commit it as the first implementation commit",
      "commit_message": "feat(review): plan-first session bootstrap",
      "done_when": "Action plan is loaded/generated and committed as first commit",
      "unit_tests": [
        "tests/unit/test_review_dev_runner.py::test_load_or_generate_plan"
      ]
    },
    {
      "index": 2,
      "title": "Implement step execution loop",
      "files": [
        "src/ferova/review/dev_runner.py"
      ],
      "action": "Add the per-step execution loop with gates (syntax, ruff, unit tests) and one-retry mechanism",
      "commit_message": "feat(review): per-step executor with gates",
      "done_when": "Developer can execute plan steps with proper validation and retry logic",
      "unit_tests": [
        "tests/unit/test_review_plan_executor.py::test_execute_plan_step",
        "tests/unit/test_review_plan_executor.py::test_execute_plan_with_failure"
      ]
    },
    {
      "index": 3,
      "title": "Implement session wrap-up",
      "files": [
        "src/ferova/review/dev_runner.py"
      ],
      "action": "Add logic to run full test suite and integration tests after all steps complete, then push and open PR",
      "commit_message": "feat(review): session wrap-up with full suite and integration tests",
      "done_when": "Full test suite and integration tests run successfully before pushing branch and opening PR",
      "unit_tests": [
        "tests/unit/test_review_dev_runner.py::test_session_wrapup"
      ]
    },
    {
      "index": 4,
      "title": "Extend DevSessionResult",
      "files": [
        "src/ferova/review/dev_runner.py"
      ],
      "action": "Add steps_completed, steps_total, failed_step_index, and plan_committed fields to DevSessionResult",
      "commit_message": "feat(review): extend DevSessionResult with execution tracking fields",
      "done_when": "DevSessionResult includes new fields for tracking execution progress",
      "unit_tests": [
        "tests/unit/test_review_dev_runner.py::test_dev_session_result_fields"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_developer_session.py"
  ]
}
```
