# SP-PLAN-CONTRACT-LINTS-2 — Discriminating promises — node ids required, integration under tests/integration

Add two validators to ActionPlan/PlanStep that reject bare-file unit_test promises (G1) and unit-tree integration_test promises (G2), then migrate all in-repo fixtures so the full suite passes under the new rules.

## Step 1 — Add G1/G2 validators with AC1-AC4 tests

- **Files**: `src/ferova/review/plan.py`, `tests/unit/test_review_plan.py`, `tests/integration/test_plan_contract_lints.py`
- **Action**: In src/ferova/review/plan.py: (a) extend PlanStep._selectors_safe (or add a new field_validator on unit_tests) to reject any selector that does not contain '::', raising ValueError with the directive 'promise the exact test function: file.py::test_name — a bare file proves nothing about this step's new work' (class-scoped selectors with multiple '::' satisfy this). (b) Add a model_validator on ActionPlan that iterates integration_tests, splits each on '::' to get the file part, and raises ValueError naming the offending selector when the file part does not start with 'tests/integration/'. In tests/unit/test_review_plan.py add four tests: test_bare_file_unit_promise_is_rejected (asserts ValidationError with 'promise the exact test function'), test_node_id_unit_promise_is_accepted (asserts validation succeeds), test_unit_path_integration_promise_is_rejected (asserts ValidationError names the selector), test_integration_tree_promise_is_accepted (asserts validation succeeds with tests/integration/test_x.py::test_e2e). In tests/integration/test_plan_contract_lints.py add test_node_id_and_integration_tree_lints_fire_on_round_trip that renders a compliant plan, mutates the json fence to use a bare-file unit_tests and a unit-tree integration_tests, and asserts parse_plan_markdown raises ValidationError with both directive messages.
- **Commit**: `feat(review-plan): require node ids and integration-tree paths`
- **Done when**: pytest tests/unit/test_review_plan.py::test_bare_file_unit_promise_is_rejected tests/unit/test_review_plan.py::test_node_id_unit_promise_is_accepted tests/unit/test_review_plan.py::test_unit_path_integration_promise_is_rejected tests/unit/test_review_plan.py::test_integration_tree_promise_is_accepted passes
- **Unit tests**: `tests/unit/test_review_plan.py::test_bare_file_unit_promise_is_rejected`, `tests/unit/test_review_plan.py::test_node_id_unit_promise_is_accepted`, `tests/unit/test_review_plan.py::test_unit_path_integration_promise_is_rejected`, `tests/unit/test_review_plan.py::test_integration_tree_promise_is_accepted`

## Step 2 — Migrate test_review_plan_executor.py fixtures

- **Files**: `tests/unit/test_review_plan_executor.py`
- **Action**: Migrate all bare-file unit_tests promises to ::node_id. Verified selectors in this file: test_value (lines 108, 1175, 1433, 1465, 1571), test_ab (line ~295), test_a and test_b (line 1490), test_promised_b (line ~1530). Update lines 295, 611, 620, 1242, 1376 accordingly. Migrate integration_tests=['tests/unit/test_mini.py'] at line 1201 to tests/integration/test_mini.py::test_value and update the fixture's tmp repo to seed tests/integration/test_mini.py with a def test_value function. Migrate integration_tests=['tests/integration/test_same_name.py'] at line 1176 to tests/integration/test_same_name.py::test_value. Negative-test selectors (test_not_written_yet, test_selector_that_does_not_exist) already use ::node_id and need no change.
- **Commit**: `test(review-plan-executor): migrate fixtures to node-id promises`
- **Done when**: pytest tests/unit/test_review_plan_executor.py passes
- **Unit tests**: `tests/unit/test_review_plan_executor.py::test_execute_plan_step`, `tests/unit/test_review_plan_executor.py::test_fix_forward_accumulates_across_attempts_without_revert`

## Step 3 — Migrate remaining unit test fixtures

- **Files**: `tests/unit/test_dev_owns_priming.py`, `tests/unit/test_dev_step_attempts.py`, `tests/unit/test_import_gate.py`, `tests/unit/test_review_dev_cli_explore_via.py`, `tests/unit/test_review_dev_runner.py`
- **Action**: Migrate bare-file unit_tests promises to ::node_id in each file. For each fixture, read the fixture body to determine the test function name it seeds in its tmp repo (the promise must reference a function the fixture actually creates). Verified: tests/unit/test_dev_owns_priming.py has test_render_owns_brief_states_allowed_deps at line 63 — use that as the representative promise for that file. For test_dev_step_attempts.py line 21, test_import_gate.py line 142, test_review_dev_cli_explore_via.py line 73, test_review_dev_runner.py line 184 — read each fixture's _one_step_plan or equivalent helper to find the seeded test function name and append ::<that_name>. No integration_tests changes needed in these files (they already point to tests/integration/...).
- **Commit**: `test(review): migrate unit fixtures to node-id promises`
- **Done when**: pytest tests/unit/test_dev_owns_priming.py tests/unit/test_dev_step_attempts.py tests/unit/test_import_gate.py tests/unit/test_review_dev_cli_explore_via.py tests/unit/test_review_dev_runner.py passes
- **Unit tests**: `tests/unit/test_dev_owns_priming.py::test_render_owns_brief_states_allowed_deps`

## Step 4 — Migrate integration test fixtures

- **Files**: `tests/integration/test_dev_runner_preflight.py`, `tests/integration/test_dev_runner_promise_delivery.py`, `tests/integration/test_developer_session.py`, `tests/integration/test_plan_contract_lints.py`
- **Action**: Migrate bare-file unit_tests promises to ::node_id in each integration test fixture. For test_dev_runner_preflight.py line 53, test_developer_session.py lines 51 and 64, test_plan_contract_lints.py line 27 — read each fixture to determine the seeded test function name in its tmp repo and append ::<that_name>. For test_dev_runner_promise_delivery.py line 75 (integration_tests=['tests/integration/test_demo_flow.py']), migrate to tests/integration/test_demo_flow.py::test_flow and verify the fixture seeds that function. Also migrate the bare-file unit_tests at lines 108, 136, 170 to ::test_value.
- **Commit**: `test(integration): migrate fixtures to node-id promises`
- **Done when**: pytest tests/integration/test_dev_runner_preflight.py tests/integration/test_dev_runner_promise_delivery.py tests/integration/test_developer_session.py tests/integration/test_plan_contract_lints.py passes
- **Unit tests**: `tests/integration/test_plan_contract_lints.py::test_integration_promise_lint_fires_on_round_trip`

## Integration tests

- `tests/integration/test_plan_contract_lints.py::test_node_id_and_integration_tree_lints_fire_on_round_trip`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-PLAN-CONTRACT-LINTS-2",
  "title": "Discriminating promises — node ids required, integration under tests/integration",
  "summary": "Add two validators to ActionPlan/PlanStep that reject bare-file unit_test promises (G1) and unit-tree integration_test promises (G2), then migrate all in-repo fixtures so the full suite passes under the new rules.",
  "steps": [
    {
      "index": 1,
      "title": "Add G1/G2 validators with AC1-AC4 tests",
      "files": [
        "src/ferova/review/plan.py",
        "tests/unit/test_review_plan.py",
        "tests/integration/test_plan_contract_lints.py"
      ],
      "action": "In src/ferova/review/plan.py: (a) extend PlanStep._selectors_safe (or add a new field_validator on unit_tests) to reject any selector that does not contain '::', raising ValueError with the directive 'promise the exact test function: file.py::test_name — a bare file proves nothing about this step's new work' (class-scoped selectors with multiple '::' satisfy this). (b) Add a model_validator on ActionPlan that iterates integration_tests, splits each on '::' to get the file part, and raises ValueError naming the offending selector when the file part does not start with 'tests/integration/'. In tests/unit/test_review_plan.py add four tests: test_bare_file_unit_promise_is_rejected (asserts ValidationError with 'promise the exact test function'), test_node_id_unit_promise_is_accepted (asserts validation succeeds), test_unit_path_integration_promise_is_rejected (asserts ValidationError names the selector), test_integration_tree_promise_is_accepted (asserts validation succeeds with tests/integration/test_x.py::test_e2e). In tests/integration/test_plan_contract_lints.py add test_node_id_and_integration_tree_lints_fire_on_round_trip that renders a compliant plan, mutates the json fence to use a bare-file unit_tests and a unit-tree integration_tests, and asserts parse_plan_markdown raises ValidationError with both directive messages.",
      "commit_message": "feat(review-plan): require node ids and integration-tree paths",
      "done_when": "pytest tests/unit/test_review_plan.py::test_bare_file_unit_promise_is_rejected tests/unit/test_review_plan.py::test_node_id_unit_promise_is_accepted tests/unit/test_review_plan.py::test_unit_path_integration_promise_is_rejected tests/unit/test_review_plan.py::test_integration_tree_promise_is_accepted passes",
      "unit_tests": [
        "tests/unit/test_review_plan.py::test_bare_file_unit_promise_is_rejected",
        "tests/unit/test_review_plan.py::test_node_id_unit_promise_is_accepted",
        "tests/unit/test_review_plan.py::test_unit_path_integration_promise_is_rejected",
        "tests/unit/test_review_plan.py::test_integration_tree_promise_is_accepted"
      ]
    },
    {
      "index": 2,
      "title": "Migrate test_review_plan_executor.py fixtures",
      "files": [
        "tests/unit/test_review_plan_executor.py"
      ],
      "action": "Migrate all bare-file unit_tests promises to ::node_id. Verified selectors in this file: test_value (lines 108, 1175, 1433, 1465, 1571), test_ab (line ~295), test_a and test_b (line 1490), test_promised_b (line ~1530). Update lines 295, 611, 620, 1242, 1376 accordingly. Migrate integration_tests=['tests/unit/test_mini.py'] at line 1201 to tests/integration/test_mini.py::test_value and update the fixture's tmp repo to seed tests/integration/test_mini.py with a def test_value function. Migrate integration_tests=['tests/integration/test_same_name.py'] at line 1176 to tests/integration/test_same_name.py::test_value. Negative-test selectors (test_not_written_yet, test_selector_that_does_not_exist) already use ::node_id and need no change.",
      "commit_message": "test(review-plan-executor): migrate fixtures to node-id promises",
      "done_when": "pytest tests/unit/test_review_plan_executor.py passes",
      "unit_tests": [
        "tests/unit/test_review_plan_executor.py::test_execute_plan_step",
        "tests/unit/test_review_plan_executor.py::test_fix_forward_accumulates_across_attempts_without_revert"
      ]
    },
    {
      "index": 3,
      "title": "Migrate remaining unit test fixtures",
      "files": [
        "tests/unit/test_dev_owns_priming.py",
        "tests/unit/test_dev_step_attempts.py",
        "tests/unit/test_import_gate.py",
        "tests/unit/test_review_dev_cli_explore_via.py",
        "tests/unit/test_review_dev_runner.py"
      ],
      "action": "Migrate bare-file unit_tests promises to ::node_id in each file. For each fixture, read the fixture body to determine the test function name it seeds in its tmp repo (the promise must reference a function the fixture actually creates). Verified: tests/unit/test_dev_owns_priming.py has test_render_owns_brief_states_allowed_deps at line 63 — use that as the representative promise for that file. For test_dev_step_attempts.py line 21, test_import_gate.py line 142, test_review_dev_cli_explore_via.py line 73, test_review_dev_runner.py line 184 — read each fixture's _one_step_plan or equivalent helper to find the seeded test function name and append ::<that_name>. No integration_tests changes needed in these files (they already point to tests/integration/...).",
      "commit_message": "test(review): migrate unit fixtures to node-id promises",
      "done_when": "pytest tests/unit/test_dev_owns_priming.py tests/unit/test_dev_step_attempts.py tests/unit/test_import_gate.py tests/unit/test_review_dev_cli_explore_via.py tests/unit/test_review_dev_runner.py passes",
      "unit_tests": [
        "tests/unit/test_dev_owns_priming.py::test_render_owns_brief_states_allowed_deps"
      ]
    },
    {
      "index": 4,
      "title": "Migrate integration test fixtures",
      "files": [
        "tests/integration/test_dev_runner_preflight.py",
        "tests/integration/test_dev_runner_promise_delivery.py",
        "tests/integration/test_developer_session.py",
        "tests/integration/test_plan_contract_lints.py"
      ],
      "action": "Migrate bare-file unit_tests promises to ::node_id in each integration test fixture. For test_dev_runner_preflight.py line 53, test_developer_session.py lines 51 and 64, test_plan_contract_lints.py line 27 — read each fixture to determine the seeded test function name in its tmp repo and append ::<that_name>. For test_dev_runner_promise_delivery.py line 75 (integration_tests=['tests/integration/test_demo_flow.py']), migrate to tests/integration/test_demo_flow.py::test_flow and verify the fixture seeds that function. Also migrate the bare-file unit_tests at lines 108, 136, 170 to ::test_value.",
      "commit_message": "test(integration): migrate fixtures to node-id promises",
      "done_when": "pytest tests/integration/test_dev_runner_preflight.py tests/integration/test_dev_runner_promise_delivery.py tests/integration/test_developer_session.py tests/integration/test_plan_contract_lints.py passes",
      "unit_tests": [
        "tests/integration/test_plan_contract_lints.py::test_integration_promise_lint_fires_on_round_trip"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_plan_contract_lints.py::test_node_id_and_integration_tree_lints_fire_on_round_trip"
  ]
}
```
