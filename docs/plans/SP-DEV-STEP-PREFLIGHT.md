# SP-DEV-STEP-PREFLIGHT — Mechanical step preflight — skip the Developer when a step is already complete

Add a pure `step_preflight_complete(repo_root, plan, step) -> bool` predicate to `dev_runner.py` that returns True exactly when every file in `step.files` exists and the step's promised selectors (its `unit_tests` plus plan-level `integration_tests` whose file path lives in `step.files`) are green via `run_promised_tests`. Wire the predicate into `run_developer_session` after the `_step_already_committed` fast path: a preflight-complete step commits any uncommitted work on its contract files with `step.commit_message`, increments `steps_completed`, logs `dev_runner.step_preflight_complete`, records a zero-token audit row via `record_coder_response` (`pr_number=0`, `model_used="preflight"`, `tokens_used=0`), and is never dispatched to the Developer.

## Step 1 — Add the step_preflight_complete predicate

- **Files**: `src/ferova/review/dev_runner.py`, `tests/unit/test_review_plan_executor.py`
- **Action**: In `src/ferova/review/dev_runner.py`, add a module-level pure function `step_preflight_complete(repo_root: Path, plan: ActionPlan, step: PlanStep) -> bool` placed near `_step_already_committed`. Build the selector set as `list(step.unit_tests)` plus every entry in `plan.integration_tests` whose file path (the substring before `::`, or the whole selector when no `::` is present) is contained in `step.files`. Return False when the selector set is empty (nothing mechanical to prove). Return False when any path in `step.files` is not an existing file under `repo_root`. Otherwise call `run_promised_tests(repo_root, selectors)` inside a broad try/except — any exception (pytest crash, timeout, git error) logs `dev_runner.step_preflight_error` with the error string and returns False (fail-open to a normal dispatch). Return the boolean first element of the tuple on success. Add a Google-style docstring describing the inputs, the selector-attribution rule, and the fail-open contract. In `tests/unit/test_review_plan_executor.py`, add a `TestStepPreflightPredicate` class with tests that exercise the predicate directly against a tmp git repo: (a) `test_preflight_predicate_returns_false_when_a_contract_file_is_missing` — write the test file but not the module, assert False; (b) `test_preflight_predicate_returns_false_on_empty_selectors` — a step with empty `unit_tests` and no attributed integration selectors returns False even when all files exist; (c) `test_preflight_predicate_returns_true_when_files_and_tests_green` — seed both files with passing content, assert True; (d) `test_preflight_predicate_returns_false_when_promised_test_fails` — seed a failing test, assert False; (e) `test_preflight_predicate_attributes_integration_selectors_by_file` — a plan-level integration selector whose file path is in `step.files` is required (red integration test → False; green → True), and a selector whose file is NOT in `step.files` is ignored (empty attributed set with empty `unit_tests` → False).
- **Commit**: `feat(dev_runner): add step_preflight_complete predicate`
- **Done when**: pytest tests/unit/test_review_plan_executor.py::TestStepPreflightPredicate passes
- **Unit tests**: `tests/unit/test_review_plan_executor.py::TestStepPreflightPredicate::test_preflight_predicate_returns_false_when_a_contract_file_is_missing`, `tests/unit/test_review_plan_executor.py::TestStepPreflightPredicate::test_preflight_predicate_returns_false_on_empty_selectors`, `tests/unit/test_review_plan_executor.py::TestStepPreflightPredicate::test_preflight_predicate_returns_true_when_files_and_tests_green`, `tests/unit/test_review_plan_executor.py::TestStepPreflightPredicate::test_preflight_predicate_returns_false_when_promised_test_fails`, `tests/unit/test_review_plan_executor.py::TestStepPreflightPredicate::test_preflight_predicate_attributes_integration_selectors_by_file`

## Step 2 — Wire preflight into run_developer_session

- **Files**: `src/ferova/review/dev_runner.py`, `tests/unit/test_review_plan_executor.py`
- **Action**: In `src/ferova/review/dev_runner.py`, inside `run_developer_session`'s per-step loop, immediately after the existing `_step_already_committed(repo, step)` fast-path branch (around line 997), add a preflight branch: call `step_preflight_complete(repo, plan, step)`. When it returns True, (1) compute the set of contract files that differ from HEAD (reuse the existing `_changed_paths` helper or a small `git diff --name-only HEAD` filter against `step.files`); if any exist, call `commit_paths(repo, those_paths, step.commit_message)` to land them with the step's own message; (2) increment `steps_completed`; (3) call `_log.info("dev_runner.step_preflight_complete", step_index=step.index, selectors=...)`; (4) call `record_coder_response(db, pr_number=0, model_used="preflight", tokens_used=0, summary=f"preflight-complete step {step.index} ({step.title}): {', '.join(selectors)}")`; (5) `continue` to the next step without dispatching the Developer. When the predicate returns False, fall through to the existing dispatch path unchanged. Do not modify `execute_plan_step`, the attempt loop, or the contract-escape check. In `tests/unit/test_review_plan_executor.py`, add four session-level tests using the existing `_init_repo`, `_one_step_plan`, `_seed_plan`, and `_developer_writing` helpers: (a) `test_preflight_completes_a_green_step_for_zero_tokens` — seed the repo with the step's files already written and committed on a branch off `develop`, run `run_developer_session` with a Developer fake, assert `result.steps_completed == 1`, assert `developer.develop_step` was never called, and assert a `pr_coder_responses` row exists with `model_used="preflight"` and `tokens_used=0`; (b) `test_preflight_dispatches_when_a_contract_file_is_missing` — promised tests green but one `step.files` path absent → the Developer fake IS called; (c) `test_preflight_commits_uncommitted_green_work` — green work on disk, uncommitted → the step's commit subject appears in the branch log and the Developer fake is never called; (d) `test_preflight_attributes_integration_selectors_by_file` — a plan-level integration selector living in the step's files is required by the predicate (red integration test → dispatch).
- **Commit**: `feat(dev_runner): wire preflight into run_developer_session`
- **Done when**: pytest tests/unit/test_review_plan_executor.py -k preflight passes
- **Unit tests**: `tests/unit/test_review_plan_executor.py::test_preflight_completes_a_green_step_for_zero_tokens`, `tests/unit/test_review_plan_executor.py::test_preflight_dispatches_when_a_contract_file_is_missing`, `tests/unit/test_review_plan_executor.py::test_preflight_commits_uncommitted_green_work`, `tests/unit/test_review_plan_executor.py::test_preflight_attributes_integration_selectors_by_file`

## Step 3 — Integration test for the preflight skip path

- **Files**: `tests/integration/test_dev_runner_preflight.py`, `tests/unit/test_review_plan_executor.py`
- **Action**: Create `tests/integration/test_dev_runner_preflight.py` with an end-to-end integration test that exercises the full preflight skip path against a real git repo and a real pytest invocation: (1) initialize a tmp git repo with `develop` as the base branch; (2) create a branch off `develop`; (3) write a minimal `src/ferova/review/_preflight_marker.py` module and a corresponding `tests/unit/test_preflight_marker.py` that both pass; (4) commit them on the branch with a message that does NOT match the step's `commit_message` (simulating work absorbed into an earlier commit); (5) construct an `ActionPlan` with one step whose `files` includes both paths, whose `unit_tests` includes the test selector, and whose `commit_message` is the spec's step message; (6) run `run_developer_session` with a Developer fake that records any calls; (7) assert `result.steps_completed == 1`, assert the Developer fake was never invoked, and assert a `pr_coder_responses` row with `model_used="preflight"` and `tokens_used=0` was recorded. Also add a unit test `test_preflight_integration_test_file_exists` in `tests/unit/test_review_plan_executor.py` that asserts the integration test file `tests/integration/test_dev_runner_preflight.py` exists and contains the expected test function name, guarding against accidental deletion.
- **Commit**: `test(dev_runner): add integration test for preflight skip path`
- **Done when**: pytest tests/integration/test_dev_runner_preflight.py passes and pytest tests/unit/test_review_plan_executor.py::test_preflight_integration_test_file_exists passes
- **Unit tests**: `tests/unit/test_review_plan_executor.py::test_preflight_integration_test_file_exists`

## Integration tests

- `tests/integration/test_dev_runner_preflight.py`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-DEV-STEP-PREFLIGHT",
  "title": "Mechanical step preflight — skip the Developer when a step is already complete",
  "summary": "Add a pure `step_preflight_complete(repo_root, plan, step) -> bool` predicate to `dev_runner.py` that returns True exactly when every file in `step.files` exists and the step's promised selectors (its `unit_tests` plus plan-level `integration_tests` whose file path lives in `step.files`) are green via `run_promised_tests`. Wire the predicate into `run_developer_session` after the `_step_already_committed` fast path: a preflight-complete step commits any uncommitted work on its contract files with `step.commit_message`, increments `steps_completed`, logs `dev_runner.step_preflight_complete`, records a zero-token audit row via `record_coder_response` (`pr_number=0`, `model_used=\"preflight\"`, `tokens_used=0`), and is never dispatched to the Developer.",
  "steps": [
    {
      "index": 1,
      "title": "Add the step_preflight_complete predicate",
      "files": [
        "src/ferova/review/dev_runner.py",
        "tests/unit/test_review_plan_executor.py"
      ],
      "action": "In `src/ferova/review/dev_runner.py`, add a module-level pure function `step_preflight_complete(repo_root: Path, plan: ActionPlan, step: PlanStep) -> bool` placed near `_step_already_committed`. Build the selector set as `list(step.unit_tests)` plus every entry in `plan.integration_tests` whose file path (the substring before `::`, or the whole selector when no `::` is present) is contained in `step.files`. Return False when the selector set is empty (nothing mechanical to prove). Return False when any path in `step.files` is not an existing file under `repo_root`. Otherwise call `run_promised_tests(repo_root, selectors)` inside a broad try/except — any exception (pytest crash, timeout, git error) logs `dev_runner.step_preflight_error` with the error string and returns False (fail-open to a normal dispatch). Return the boolean first element of the tuple on success. Add a Google-style docstring describing the inputs, the selector-attribution rule, and the fail-open contract. In `tests/unit/test_review_plan_executor.py`, add a `TestStepPreflightPredicate` class with tests that exercise the predicate directly against a tmp git repo: (a) `test_preflight_predicate_returns_false_when_a_contract_file_is_missing` — write the test file but not the module, assert False; (b) `test_preflight_predicate_returns_false_on_empty_selectors` — a step with empty `unit_tests` and no attributed integration selectors returns False even when all files exist; (c) `test_preflight_predicate_returns_true_when_files_and_tests_green` — seed both files with passing content, assert True; (d) `test_preflight_predicate_returns_false_when_promised_test_fails` — seed a failing test, assert False; (e) `test_preflight_predicate_attributes_integration_selectors_by_file` — a plan-level integration selector whose file path is in `step.files` is required (red integration test → False; green → True), and a selector whose file is NOT in `step.files` is ignored (empty attributed set with empty `unit_tests` → False).",
      "commit_message": "feat(dev_runner): add step_preflight_complete predicate",
      "done_when": "pytest tests/unit/test_review_plan_executor.py::TestStepPreflightPredicate passes",
      "unit_tests": [
        "tests/unit/test_review_plan_executor.py::TestStepPreflightPredicate::test_preflight_predicate_returns_false_when_a_contract_file_is_missing",
        "tests/unit/test_review_plan_executor.py::TestStepPreflightPredicate::test_preflight_predicate_returns_false_on_empty_selectors",
        "tests/unit/test_review_plan_executor.py::TestStepPreflightPredicate::test_preflight_predicate_returns_true_when_files_and_tests_green",
        "tests/unit/test_review_plan_executor.py::TestStepPreflightPredicate::test_preflight_predicate_returns_false_when_promised_test_fails",
        "tests/unit/test_review_plan_executor.py::TestStepPreflightPredicate::test_preflight_predicate_attributes_integration_selectors_by_file"
      ]
    },
    {
      "index": 2,
      "title": "Wire preflight into run_developer_session",
      "files": [
        "src/ferova/review/dev_runner.py",
        "tests/unit/test_review_plan_executor.py"
      ],
      "action": "In `src/ferova/review/dev_runner.py`, inside `run_developer_session`'s per-step loop, immediately after the existing `_step_already_committed(repo, step)` fast-path branch (around line 997), add a preflight branch: call `step_preflight_complete(repo, plan, step)`. When it returns True, (1) compute the set of contract files that differ from HEAD (reuse the existing `_changed_paths` helper or a small `git diff --name-only HEAD` filter against `step.files`); if any exist, call `commit_paths(repo, those_paths, step.commit_message)` to land them with the step's own message; (2) increment `steps_completed`; (3) call `_log.info(\"dev_runner.step_preflight_complete\", step_index=step.index, selectors=...)`; (4) call `record_coder_response(db, pr_number=0, model_used=\"preflight\", tokens_used=0, summary=f\"preflight-complete step {step.index} ({step.title}): {', '.join(selectors)}\")`; (5) `continue` to the next step without dispatching the Developer. When the predicate returns False, fall through to the existing dispatch path unchanged. Do not modify `execute_plan_step`, the attempt loop, or the contract-escape check. In `tests/unit/test_review_plan_executor.py`, add four session-level tests using the existing `_init_repo`, `_one_step_plan`, `_seed_plan`, and `_developer_writing` helpers: (a) `test_preflight_completes_a_green_step_for_zero_tokens` — seed the repo with the step's files already written and committed on a branch off `develop`, run `run_developer_session` with a Developer fake, assert `result.steps_completed == 1`, assert `developer.develop_step` was never called, and assert a `pr_coder_responses` row exists with `model_used=\"preflight\"` and `tokens_used=0`; (b) `test_preflight_dispatches_when_a_contract_file_is_missing` — promised tests green but one `step.files` path absent → the Developer fake IS called; (c) `test_preflight_commits_uncommitted_green_work` — green work on disk, uncommitted → the step's commit subject appears in the branch log and the Developer fake is never called; (d) `test_preflight_attributes_integration_selectors_by_file` — a plan-level integration selector living in the step's files is required by the predicate (red integration test → dispatch).",
      "commit_message": "feat(dev_runner): wire preflight into run_developer_session",
      "done_when": "pytest tests/unit/test_review_plan_executor.py -k preflight passes",
      "unit_tests": [
        "tests/unit/test_review_plan_executor.py::test_preflight_completes_a_green_step_for_zero_tokens",
        "tests/unit/test_review_plan_executor.py::test_preflight_dispatches_when_a_contract_file_is_missing",
        "tests/unit/test_review_plan_executor.py::test_preflight_commits_uncommitted_green_work",
        "tests/unit/test_review_plan_executor.py::test_preflight_attributes_integration_selectors_by_file"
      ]
    },
    {
      "index": 3,
      "title": "Integration test for the preflight skip path",
      "files": [
        "tests/integration/test_dev_runner_preflight.py",
        "tests/unit/test_review_plan_executor.py"
      ],
      "action": "Create `tests/integration/test_dev_runner_preflight.py` with an end-to-end integration test that exercises the full preflight skip path against a real git repo and a real pytest invocation: (1) initialize a tmp git repo with `develop` as the base branch; (2) create a branch off `develop`; (3) write a minimal `src/ferova/review/_preflight_marker.py` module and a corresponding `tests/unit/test_preflight_marker.py` that both pass; (4) commit them on the branch with a message that does NOT match the step's `commit_message` (simulating work absorbed into an earlier commit); (5) construct an `ActionPlan` with one step whose `files` includes both paths, whose `unit_tests` includes the test selector, and whose `commit_message` is the spec's step message; (6) run `run_developer_session` with a Developer fake that records any calls; (7) assert `result.steps_completed == 1`, assert the Developer fake was never invoked, and assert a `pr_coder_responses` row with `model_used=\"preflight\"` and `tokens_used=0` was recorded. Also add a unit test `test_preflight_integration_test_file_exists` in `tests/unit/test_review_plan_executor.py` that asserts the integration test file `tests/integration/test_dev_runner_preflight.py` exists and contains the expected test function name, guarding against accidental deletion.",
      "commit_message": "test(dev_runner): add integration test for preflight skip path",
      "done_when": "pytest tests/integration/test_dev_runner_preflight.py passes and pytest tests/unit/test_review_plan_executor.py::test_preflight_integration_test_file_exists passes",
      "unit_tests": [
        "tests/unit/test_review_plan_executor.py::test_preflight_integration_test_file_exists"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_dev_runner_preflight.py"
  ]
}
```
