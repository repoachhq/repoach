# SP-DEV-PROMISE-DELIVERY — Promised tests delivered — strict reconciliation and mechanical rename

Tighten the step gate in execute_plan_step so a reconciled green is accepted only when the loop actually touched the promised test file in this attempt (G1), and when the drift is unambiguous (exactly one missing promised node id and exactly one unpromised test function in the touched file) the runner mechanically renames the delivered function to the promised name and re-runs the exact selectors strictly (G2). Ambiguous drifts keep today's reconciled-accept with its warning log.

## Step 1 — Add gate helpers for promised-file touch detection and mechanical rename

- **Files**: `src/ferova/review/dev_runner.py`, `tests/unit/test_dev_runner_promise_helpers.py`
- **Action**: Add two module-level helpers in src/ferova/review/dev_runner.py: (a) `_promised_test_files(unit_tests: list[str]) -> set[str]` returning the set of repo-relative file paths extracted from the promised selectors (split on '::', take [0], skip bare-file selectors that have no '::'); (b) `_attempt_mechanical_rename(repo_root: Path, file_path: str, promised: list[str], delivered: list[str]) -> tuple[bool, str]` that, when exactly one promised node id is missing from the file and exactly one test function is present in the file that no plan step promises, reads the file, renames that single `def <delivered>(...)` to `def <promised>(...)` (preserving decorators and body), writes it back, and returns `(True, promised_name)`; otherwise returns `(False, '')`. On any parse/IO error, restore the original content and return `(False, '')`. Add unit tests in tests/unit/test_dev_runner_promise_helpers.py covering: empty promised list, bare-file selectors (no '::'), single missing + single candidate rename success, ambiguous (two missing) no-rename, ambiguous (two candidates) no-rename, parse error restores content.
- **Commit**: `feat(review): add promise-delivery gate helpers`
- **Done when**: pytest tests/unit/test_dev_runner_promise_helpers.py passes
- **Unit tests**: `tests/unit/test_dev_runner_promise_helpers.py::test_promised_test_files_extracts_paths`, `tests/unit/test_dev_runner_promise_helpers.py::test_promised_test_files_skips_bare_file_selectors`, `tests/unit/test_dev_runner_promise_helpers.py::test_attempt_mechanical_rename_single_drift_renames`, `tests/unit/test_dev_runner_promise_helpers.py::test_attempt_mechanical_rename_ambiguous_missing_no_rename`, `tests/unit/test_dev_runner_promise_helpers.py::test_attempt_mechanical_rename_ambiguous_candidates_no_rename`, `tests/unit/test_dev_runner_promise_helpers.py::test_attempt_mechanical_rename_parse_error_restores`

## Step 2 — Enforce G1 and G2 in the step gate of execute_plan_step

- **Files**: `src/ferova/review/dev_runner.py`, `tests/unit/test_review_plan_executor.py`
- **Action**: In execute_plan_step, after the existing `tests_ok, tests_tail, reconciled = run_promised_tests(...)` block and BEFORE the existing `if reconciled:` warning log, insert the G1/G2 logic: compute `touched_promised = _promised_test_files(step.unit_tests) & set(changed)`; if `reconciled and not touched_promised`, set `gate_feedback = ("promised-test gate: reconciled green but the loop did not touch the promised test file(s) in this attempt — write tests named exactly: " + ", ".join(step.unit_tests))` and `continue` (retryable). Else if `reconciled`, call `_attempt_mechanical_rename(repo_root, file_path, promised_list, delivered_list)` for each touched promised file; on success, re-run `run_promised_tests(repo_root, list(step.unit_tests))` strictly — if still green, proceed (drop the reconciled warning); if red, restore content and fall back to the G1 retryable feedback. Keep the existing `if reconciled:` warning log for the ambiguous-drift case (no rename applied). Add the three AC tests in tests/unit/test_review_plan_executor.py: test_untouched_promised_file_reconciliation_is_retried (AC1 — two dispatches: first attempt writes only source code while the promised test file pre-exists green, gate feedback names missing selectors; second attempt writes them, step goes green), test_touched_file_with_drifted_name_is_renamed_to_promise (AC2 — single drifted test in a touched file, step green in one attempt, promised node id exists in committed file), test_ambiguous_drift_keeps_reconciled_accept (AC3 — two missing + two candidates, no rename, step green via reconciled-accept).
- **Commit**: `feat(review): enforce strict promise delivery at step gate`
- **Done when**: pytest tests/unit/test_review_plan_executor.py::test_untouched_promised_file_reconciliation_is_retried tests/unit/test_review_plan_executor.py::test_touched_file_with_drifted_name_is_renamed_to_promise tests/unit/test_review_plan_executor.py::test_ambiguous_drift_keeps_reconciled_accept pass
- **Unit tests**: `tests/unit/test_review_plan_executor.py::test_untouched_promised_file_reconciliation_is_retried`, `tests/unit/test_review_plan_executor.py::test_touched_file_with_drifted_name_is_renamed_to_promise`, `tests/unit/test_review_plan_executor.py::test_ambiguous_drift_keeps_reconciled_accept`

## Step 3 — Add integration test for the full promise-delivery gate flow

- **Files**: `tests/integration/test_dev_runner_promise_delivery.py`
- **Action**: Add tests/integration/test_dev_runner_promise_delivery.py exercising execute_plan_step end-to-end against a temp git repo: (a) nominal case — Developer writes the promised test file with exact names, step green in one attempt; (b) G1 case — Developer writes only source code while the promised test file pre-exists green, first attempt returns ok=False with gate_feedback naming the missing selectors, second attempt that writes them returns ok=True; (c) G2 case — Developer writes the promised file with a single drifted test name, step green in one attempt and the promised node id is present in the committed file. Use the existing fake Developer pattern from tests/unit/test_review_plan_executor.py.
- **Commit**: `test(review): integration coverage for promise-delivery gate`
- **Done when**: pytest tests/integration/test_dev_runner_promise_delivery.py passes
- **Unit tests**: `tests/integration/test_dev_runner_promise_delivery.py::test_nominal_exact_names_green_first_attempt`, `tests/integration/test_dev_runner_promise_delivery.py::test_untouched_promised_file_retries_then_green`, `tests/integration/test_dev_runner_promise_delivery.py::test_single_drift_renamed_and_green`

## Integration tests

- `tests/integration/test_dev_runner_promise_delivery.py`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-DEV-PROMISE-DELIVERY",
  "title": "Promised tests delivered — strict reconciliation and mechanical rename",
  "summary": "Tighten the step gate in execute_plan_step so a reconciled green is accepted only when the loop actually touched the promised test file in this attempt (G1), and when the drift is unambiguous (exactly one missing promised node id and exactly one unpromised test function in the touched file) the runner mechanically renames the delivered function to the promised name and re-runs the exact selectors strictly (G2). Ambiguous drifts keep today's reconciled-accept with its warning log.",
  "steps": [
    {
      "index": 1,
      "title": "Add gate helpers for promised-file touch detection and mechanical rename",
      "files": [
        "src/ferova/review/dev_runner.py",
        "tests/unit/test_dev_runner_promise_helpers.py"
      ],
      "action": "Add two module-level helpers in src/ferova/review/dev_runner.py: (a) `_promised_test_files(unit_tests: list[str]) -> set[str]` returning the set of repo-relative file paths extracted from the promised selectors (split on '::', take [0], skip bare-file selectors that have no '::'); (b) `_attempt_mechanical_rename(repo_root: Path, file_path: str, promised: list[str], delivered: list[str]) -> tuple[bool, str]` that, when exactly one promised node id is missing from the file and exactly one test function is present in the file that no plan step promises, reads the file, renames that single `def <delivered>(...)` to `def <promised>(...)` (preserving decorators and body), writes it back, and returns `(True, promised_name)`; otherwise returns `(False, '')`. On any parse/IO error, restore the original content and return `(False, '')`. Add unit tests in tests/unit/test_dev_runner_promise_helpers.py covering: empty promised list, bare-file selectors (no '::'), single missing + single candidate rename success, ambiguous (two missing) no-rename, ambiguous (two candidates) no-rename, parse error restores content.",
      "commit_message": "feat(review): add promise-delivery gate helpers",
      "done_when": "pytest tests/unit/test_dev_runner_promise_helpers.py passes",
      "unit_tests": [
        "tests/unit/test_dev_runner_promise_helpers.py::test_promised_test_files_extracts_paths",
        "tests/unit/test_dev_runner_promise_helpers.py::test_promised_test_files_skips_bare_file_selectors",
        "tests/unit/test_dev_runner_promise_helpers.py::test_attempt_mechanical_rename_single_drift_renames",
        "tests/unit/test_dev_runner_promise_helpers.py::test_attempt_mechanical_rename_ambiguous_missing_no_rename",
        "tests/unit/test_dev_runner_promise_helpers.py::test_attempt_mechanical_rename_ambiguous_candidates_no_rename",
        "tests/unit/test_dev_runner_promise_helpers.py::test_attempt_mechanical_rename_parse_error_restores"
      ]
    },
    {
      "index": 2,
      "title": "Enforce G1 and G2 in the step gate of execute_plan_step",
      "files": [
        "src/ferova/review/dev_runner.py",
        "tests/unit/test_review_plan_executor.py"
      ],
      "action": "In execute_plan_step, after the existing `tests_ok, tests_tail, reconciled = run_promised_tests(...)` block and BEFORE the existing `if reconciled:` warning log, insert the G1/G2 logic: compute `touched_promised = _promised_test_files(step.unit_tests) & set(changed)`; if `reconciled and not touched_promised`, set `gate_feedback = (\"promised-test gate: reconciled green but the loop did not touch the promised test file(s) in this attempt — write tests named exactly: \" + \", \".join(step.unit_tests))` and `continue` (retryable). Else if `reconciled`, call `_attempt_mechanical_rename(repo_root, file_path, promised_list, delivered_list)` for each touched promised file; on success, re-run `run_promised_tests(repo_root, list(step.unit_tests))` strictly — if still green, proceed (drop the reconciled warning); if red, restore content and fall back to the G1 retryable feedback. Keep the existing `if reconciled:` warning log for the ambiguous-drift case (no rename applied). Add the three AC tests in tests/unit/test_review_plan_executor.py: test_untouched_promised_file_reconciliation_is_retried (AC1 — two dispatches: first attempt writes only source code while the promised test file pre-exists green, gate feedback names missing selectors; second attempt writes them, step goes green), test_touched_file_with_drifted_name_is_renamed_to_promise (AC2 — single drifted test in a touched file, step green in one attempt, promised node id exists in committed file), test_ambiguous_drift_keeps_reconciled_accept (AC3 — two missing + two candidates, no rename, step green via reconciled-accept).",
      "commit_message": "feat(review): enforce strict promise delivery at step gate",
      "done_when": "pytest tests/unit/test_review_plan_executor.py::test_untouched_promised_file_reconciliation_is_retried tests/unit/test_review_plan_executor.py::test_touched_file_with_drifted_name_is_renamed_to_promise tests/unit/test_review_plan_executor.py::test_ambiguous_drift_keeps_reconciled_accept pass",
      "unit_tests": [
        "tests/unit/test_review_plan_executor.py::test_untouched_promised_file_reconciliation_is_retried",
        "tests/unit/test_review_plan_executor.py::test_touched_file_with_drifted_name_is_renamed_to_promise",
        "tests/unit/test_review_plan_executor.py::test_ambiguous_drift_keeps_reconciled_accept"
      ]
    },
    {
      "index": 3,
      "title": "Add integration test for the full promise-delivery gate flow",
      "files": [
        "tests/integration/test_dev_runner_promise_delivery.py"
      ],
      "action": "Add tests/integration/test_dev_runner_promise_delivery.py exercising execute_plan_step end-to-end against a temp git repo: (a) nominal case — Developer writes the promised test file with exact names, step green in one attempt; (b) G1 case — Developer writes only source code while the promised test file pre-exists green, first attempt returns ok=False with gate_feedback naming the missing selectors, second attempt that writes them returns ok=True; (c) G2 case — Developer writes the promised file with a single drifted test name, step green in one attempt and the promised node id is present in the committed file. Use the existing fake Developer pattern from tests/unit/test_review_plan_executor.py.",
      "commit_message": "test(review): integration coverage for promise-delivery gate",
      "done_when": "pytest tests/integration/test_dev_runner_promise_delivery.py passes",
      "unit_tests": [
        "tests/integration/test_dev_runner_promise_delivery.py::test_nominal_exact_names_green_first_attempt",
        "tests/integration/test_dev_runner_promise_delivery.py::test_untouched_promised_file_retries_then_green",
        "tests/integration/test_dev_runner_promise_delivery.py::test_single_drift_renamed_and_green"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_dev_runner_promise_delivery.py"
  ]
}
```
