# SP-DEV-PROMISE-RECONCILE — Pytest gate reconciles promised test ids to delivered tests

Add run_promised_tests to dev_runner.py — a three-valued wrapper around run_pytest_selectors that first tries the exact promised selectors, then falls back to the promised files when selector-level names mismatched. Wire execute_plan_step to call it instead of run_pytest_selectors directly, emit a warning-level dev_runner.promised_tests_reconciled log on reconciliation, and update the module docstring gate description. Cover all four DoD scenarios in the new file tests/unit/test_dev_promise_reconcile.py (never touching the existing 346-line test_review_dev_runner.py). Steps are split so dev_runner.py is only contracted together with a fresh new file or alone, respecting the one-big-file-per-step constraint from prior failed dispatches.

## Step 1 — Add run_promised_tests helper and create dedicated test file

- **Files**: `src/ferova/review/dev_runner.py`, `tests/unit/test_dev_promise_reconcile.py`
- **Action**: In dev_runner.py, insert the new function run_promised_tests(repo_root: Path, selectors: list[str]) -> tuple[bool, str, bool] immediately after run_pytest_selectors (after line 427). Logic: (1) call run_pytest_selectors(repo_root, selectors) on the exact selectors — if green return (True, tail, False); (2) on failure, derive file paths via sorted({s.split('::', 1)[0] for s in selectors}), call run_pytest_selectors on those file paths — if green return (True, tail, True); (3) both red → return (False, file_level_tail, False). The hostile-selector guard is inherited because both inner calls go through run_pytest_selectors. Create tests/unit/test_dev_promise_reconcile.py with three tests: test_exact_promised_ids_stay_happy_path (writes a passing test function matching the promised selector to tmp_path, calls run_promised_tests with that selector, asserts green=True and reconciled=False); test_mismatched_names_reconcile_to_delivered_tests (writes a passing test under a DIFFERENT function name than the selector, calls run_promised_tests, asserts green=True and reconciled=True); test_red_or_empty_delivered_tests_stay_red (parametrised with two cases: (a) test file exists but its test fails, (b) test file is empty — both must return (False, tail, False)). Each test writes real .py files under tmp_path and calls run_promised_tests directly.
- **Commit**: `feat(dev): add run_promised_tests with file-level fallback gate`
- **Done when**: pytest tests/unit/test_dev_promise_reconcile.py::test_exact_promised_ids_stay_happy_path tests/unit/test_dev_promise_reconcile.py::test_mismatched_names_reconcile_to_delivered_tests tests/unit/test_dev_promise_reconcile.py::test_red_or_empty_delivered_tests_stay_red passes; ruff check src/ferova/review/dev_runner.py exits 0
- **Unit tests**: `tests/unit/test_dev_promise_reconcile.py::test_exact_promised_ids_stay_happy_path`, `tests/unit/test_dev_promise_reconcile.py::test_mismatched_names_reconcile_to_delivered_tests`, `tests/unit/test_dev_promise_reconcile.py::test_red_or_empty_delivered_tests_stay_red`

## Step 2 — Wire execute_plan_step to run_promised_tests and emit reconciliation log

- **Files**: `src/ferova/review/dev_runner.py`
- **Action**: In execute_plan_step, replace the single line 'tests_ok, tests_tail = run_pytest_selectors(repo_root, list(step.unit_tests))' with 'tests_ok, tests_tail, reconciled = run_promised_tests(repo_root, list(step.unit_tests))'. Keep the immediately following 'if not tests_ok:' revert+continue block completely unchanged. After that block, and before the commit_all call, insert: 'if reconciled: _log.warning("dev_runner.promised_tests_reconciled", spec_id=plan.spec_id, step=step.index, promised=list(step.unit_tests))'. The absent-file check block above (the 'if absent:' early-return) stays untouched. Also update the module-level docstring's step-3 gate description to note that the pytest gate first attempts exact promised selectors, then falls back to promised files when selector names differ.
- **Commit**: `feat(dev): wire execute_plan_step to reconcile promised test ids`
- **Done when**: python -c 'from ferova.review.dev_runner import run_promised_tests, execute_plan_step' exits 0; pytest tests/unit/test_dev_promise_reconcile.py::test_exact_promised_ids_stay_happy_path passes; ruff check src/ferova/review/dev_runner.py exits 0
- **Unit tests**: `tests/unit/test_dev_promise_reconcile.py::test_exact_promised_ids_stay_happy_path`

## Step 3 — Add end-to-end reconciliation test for execute_plan_step

- **Files**: `tests/unit/test_dev_promise_reconcile.py`
- **Action**: Append test_step_commits_on_reconciled_tests to tests/unit/test_dev_promise_reconcile.py. The test must: (a) seed a tmp git repo using the same subprocess git-init + git-config + git-commit pattern as _init_git_repo_with_plan in test_review_dev_runner.py, with a one-step ActionPlan whose PlanStep has unit_tests=['tests/unit/test_x.py::test_promised_name'] and files=['tests/unit/test_x.py']; (b) build a MagicMock Developer whose respond() writes tests/unit/test_x.py containing 'def test_delivered_name(): pass' (mismatched function name but the file passes under the file-level fallback); (c) call execute_plan_step(step, plan=plan, repo_root=repo, developer=dev, repo_tree='', db=tmp_path/'t.db') and assert outcome.ok is True; (d) confirm a new git commit exists (run 'git log --oneline' and check count > initial); (e) capture structlog output (or monkeypatch _log.warning) and assert 'dev_runner.promised_tests_reconciled' was emitted. Import execute_plan_step, ActionPlan, PlanStep, StepOutcome from ferova.review.dev_runner and ferova.review.plan. Also import record_coder_response and init_schema for db setup, and MagicMock. Do not import anything from test_review_dev_runner.py.
- **Commit**: `test(dev): promise-reconcile end-to-end commit on reconciled step`
- **Done when**: pytest tests/unit/test_dev_promise_reconcile.py::test_step_commits_on_reconciled_tests passes; ruff check tests/unit/test_dev_promise_reconcile.py exits 0
- **Unit tests**: `tests/unit/test_dev_promise_reconcile.py::test_step_commits_on_reconciled_tests`

## Integration tests

- `tests/unit/test_dev_promise_reconcile.py::test_step_commits_on_reconciled_tests`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-DEV-PROMISE-RECONCILE",
  "title": "Pytest gate reconciles promised test ids to delivered tests",
  "summary": "Add run_promised_tests to dev_runner.py — a three-valued wrapper around run_pytest_selectors that first tries the exact promised selectors, then falls back to the promised files when selector-level names mismatched. Wire execute_plan_step to call it instead of run_pytest_selectors directly, emit a warning-level dev_runner.promised_tests_reconciled log on reconciliation, and update the module docstring gate description. Cover all four DoD scenarios in the new file tests/unit/test_dev_promise_reconcile.py (never touching the existing 346-line test_review_dev_runner.py). Steps are split so dev_runner.py is only contracted together with a fresh new file or alone, respecting the one-big-file-per-step constraint from prior failed dispatches.",
  "steps": [
    {
      "index": 1,
      "title": "Add run_promised_tests helper and create dedicated test file",
      "files": [
        "src/ferova/review/dev_runner.py",
        "tests/unit/test_dev_promise_reconcile.py"
      ],
      "action": "In dev_runner.py, insert the new function run_promised_tests(repo_root: Path, selectors: list[str]) -> tuple[bool, str, bool] immediately after run_pytest_selectors (after line 427). Logic: (1) call run_pytest_selectors(repo_root, selectors) on the exact selectors — if green return (True, tail, False); (2) on failure, derive file paths via sorted({s.split('::', 1)[0] for s in selectors}), call run_pytest_selectors on those file paths — if green return (True, tail, True); (3) both red → return (False, file_level_tail, False). The hostile-selector guard is inherited because both inner calls go through run_pytest_selectors. Create tests/unit/test_dev_promise_reconcile.py with three tests: test_exact_promised_ids_stay_happy_path (writes a passing test function matching the promised selector to tmp_path, calls run_promised_tests with that selector, asserts green=True and reconciled=False); test_mismatched_names_reconcile_to_delivered_tests (writes a passing test under a DIFFERENT function name than the selector, calls run_promised_tests, asserts green=True and reconciled=True); test_red_or_empty_delivered_tests_stay_red (parametrised with two cases: (a) test file exists but its test fails, (b) test file is empty — both must return (False, tail, False)). Each test writes real .py files under tmp_path and calls run_promised_tests directly.",
      "commit_message": "feat(dev): add run_promised_tests with file-level fallback gate",
      "done_when": "pytest tests/unit/test_dev_promise_reconcile.py::test_exact_promised_ids_stay_happy_path tests/unit/test_dev_promise_reconcile.py::test_mismatched_names_reconcile_to_delivered_tests tests/unit/test_dev_promise_reconcile.py::test_red_or_empty_delivered_tests_stay_red passes; ruff check src/ferova/review/dev_runner.py exits 0",
      "unit_tests": [
        "tests/unit/test_dev_promise_reconcile.py::test_exact_promised_ids_stay_happy_path",
        "tests/unit/test_dev_promise_reconcile.py::test_mismatched_names_reconcile_to_delivered_tests",
        "tests/unit/test_dev_promise_reconcile.py::test_red_or_empty_delivered_tests_stay_red"
      ]
    },
    {
      "index": 2,
      "title": "Wire execute_plan_step to run_promised_tests and emit reconciliation log",
      "files": [
        "src/ferova/review/dev_runner.py"
      ],
      "action": "In execute_plan_step, replace the single line 'tests_ok, tests_tail = run_pytest_selectors(repo_root, list(step.unit_tests))' with 'tests_ok, tests_tail, reconciled = run_promised_tests(repo_root, list(step.unit_tests))'. Keep the immediately following 'if not tests_ok:' revert+continue block completely unchanged. After that block, and before the commit_all call, insert: 'if reconciled: _log.warning(\"dev_runner.promised_tests_reconciled\", spec_id=plan.spec_id, step=step.index, promised=list(step.unit_tests))'. The absent-file check block above (the 'if absent:' early-return) stays untouched. Also update the module-level docstring's step-3 gate description to note that the pytest gate first attempts exact promised selectors, then falls back to promised files when selector names differ.",
      "commit_message": "feat(dev): wire execute_plan_step to reconcile promised test ids",
      "done_when": "python -c 'from ferova.review.dev_runner import run_promised_tests, execute_plan_step' exits 0; pytest tests/unit/test_dev_promise_reconcile.py::test_exact_promised_ids_stay_happy_path passes; ruff check src/ferova/review/dev_runner.py exits 0",
      "unit_tests": [
        "tests/unit/test_dev_promise_reconcile.py::test_exact_promised_ids_stay_happy_path"
      ]
    },
    {
      "index": 3,
      "title": "Add end-to-end reconciliation test for execute_plan_step",
      "files": [
        "tests/unit/test_dev_promise_reconcile.py"
      ],
      "action": "Append test_step_commits_on_reconciled_tests to tests/unit/test_dev_promise_reconcile.py. The test must: (a) seed a tmp git repo using the same subprocess git-init + git-config + git-commit pattern as _init_git_repo_with_plan in test_review_dev_runner.py, with a one-step ActionPlan whose PlanStep has unit_tests=['tests/unit/test_x.py::test_promised_name'] and files=['tests/unit/test_x.py']; (b) build a MagicMock Developer whose respond() writes tests/unit/test_x.py containing 'def test_delivered_name(): pass' (mismatched function name but the file passes under the file-level fallback); (c) call execute_plan_step(step, plan=plan, repo_root=repo, developer=dev, repo_tree='', db=tmp_path/'t.db') and assert outcome.ok is True; (d) confirm a new git commit exists (run 'git log --oneline' and check count > initial); (e) capture structlog output (or monkeypatch _log.warning) and assert 'dev_runner.promised_tests_reconciled' was emitted. Import execute_plan_step, ActionPlan, PlanStep, StepOutcome from ferova.review.dev_runner and ferova.review.plan. Also import record_coder_response and init_schema for db setup, and MagicMock. Do not import anything from test_review_dev_runner.py.",
      "commit_message": "test(dev): promise-reconcile end-to-end commit on reconciled step",
      "done_when": "pytest tests/unit/test_dev_promise_reconcile.py::test_step_commits_on_reconciled_tests passes; ruff check tests/unit/test_dev_promise_reconcile.py exits 0",
      "unit_tests": [
        "tests/unit/test_dev_promise_reconcile.py::test_step_commits_on_reconciled_tests"
      ]
    }
  ],
  "integration_tests": [
    "tests/unit/test_dev_promise_reconcile.py::test_step_commits_on_reconciled_tests"
  ]
}
```
