# SP-PLAN-CONTRACT-LINTS — Plan-form lint: integration promises must be created by some step

Add a new `model_validator(mode="after")` on `ActionPlan` that rejects any plan whose `integration_tests` selector names a file absent from the union of all steps' `files`, with a directive error message naming the selector, the missing file, and the phrase "add that file". Mirrors the existing unit-side rule but without index ordering (integration tests run at session end).

## Step 1 — Add the integration-promise validator and its first unit + integration tests

- **Files**: `src/ferova/review/plan.py`, `tests/unit/test_review_plan.py`, `tests/integration/test_plan_contract_lints.py`
- **Action**: In `src/ferova/review/plan.py`, add a new `@model_validator(mode="after")` method on `ActionPlan` (placed after `_promised_tests_are_created_by_the_plan`) named e.g. `_integration_promises_are_created_by_the_plan`. It must: (1) compute the union of every step's `files` across ALL steps (no index ordering — integration tests run at session end); (2) for each selector in `self.integration_tests`, split on `"::"` with `maxsplit=1` and take the file part; (3) if that file is not in the union, raise `ValueError` with a directive message that names the offending selector, the missing file path, and the phrase "add that file" (e.g. `f"integration test {selector!r} is promised but no step creates {test_file!r} — add that file to a step's files (the per-step jail forbids writing files outside every contract)"`). The validator must be a no-op when `integration_tests` is empty (vacuous pass). In `tests/unit/test_review_plan.py`, add `test_integration_promise_without_creating_step_is_rejected` (AC1): build a plan with one step whose `files` is `["src/ferova/feature.py", "tests/unit/test_feature.py"]` and `unit_tests=["tests/unit/test_feature.py"]`, and `integration_tests=["tests/integration/test_feature_e2e.py"]` — assert `ValidationError` is raised and the message contains the selector, the missing file path, and "add that file". Create `tests/integration/test_plan_contract_lints.py` with one test that round-trips a violating plan through `render_plan_markdown` then `parse_plan_markdown` and asserts the same `ValidationError` with the directive message — this proves the lint fires on the committed-document path, not just direct construction.
- **Commit**: `feat(review): reject integration promises no step creates`
- **Done when**: pytest tests/unit/test_review_plan.py::TestActionPlanValidation::test_integration_promise_without_creating_step_is_rejected passes and pytest tests/integration/test_plan_contract_lints.py passes
- **Unit tests**: `tests/unit/test_review_plan.py::TestActionPlanValidation::test_integration_promise_without_creating_step_is_rejected`

## Step 2 — Add acceptance tests for the new rule (any-step creation + node-id resolution)

- **Files**: `tests/unit/test_review_plan.py`
- **Action**: In `tests/unit/test_review_plan.py`, add two tests inside `TestActionPlanValidation`. (a) `test_integration_promise_created_by_any_step_is_accepted` (AC2): build a two-step plan where step 1 has `files=["src/ferova/feature.py", "tests/unit/test_feature.py"]` and `unit_tests=["tests/unit/test_feature.py"]`, and step 2 has `files=["tests/integration/test_feature_e2e.py"]` and `unit_tests=[]` (docs-only exemption — but step 2 is not docs-only, so it must promise a unit test; use a trivial unit test like `["tests/unit/test_feature.py::test_smoke"]` which already exists from step 1). Set `integration_tests=["tests/integration/test_feature_e2e.py::test_e2e"]`. Assert the plan validates. (b) `test_integration_promise_node_id_resolves_file_part` (AC3): a single-step plan with `files=["src/ferova/feature.py", "tests/integration/test_feature_e2e.py"]`, `unit_tests=["tests/integration/test_feature_e2e.py::test_smoke"]`, and `integration_tests=["tests/integration/test_feature_e2e.py::test_e2e"]` — assert it validates (the `::node` suffix is stripped before the file-part check).
- **Commit**: `test(review): cover integration-promise acceptance paths`
- **Done when**: pytest tests/unit/test_review_plan.py::TestActionPlanValidation::test_integration_promise_created_by_any_step_is_accepted tests/unit/test_review_plan.py::TestActionPlanValidation::test_integration_promise_node_id_resolves_file_part passes
- **Unit tests**: `tests/unit/test_review_plan.py::TestActionPlanValidation::test_integration_promise_created_by_any_step_is_accepted`, `tests/unit/test_review_plan.py::TestActionPlanValidation::test_integration_promise_node_id_resolves_file_part`

## Step 3 — Add regression test for the docs-only exemption under the new rule

- **Files**: `tests/unit/test_review_plan.py`
- **Action**: In `tests/unit/test_review_plan.py`, add `test_docs_only_plan_with_empty_integration_promises_stays_valid` (AC4): build a plan with a single docs-only step (`files=["docs/notes.md"]`, `unit_tests=[]`) and `integration_tests=[]`. Assert the plan validates — the new rule must be vacuously satisfied on empty `integration_tests`, and the existing src-interlock exemption at `plan.py:186-192` must remain untouched.
- **Commit**: `test(review): guard docs-only exemption under new lint`
- **Done when**: pytest tests/unit/test_review_plan.py::TestActionPlanValidation::test_docs_only_plan_with_empty_integration_promises_stays_valid passes
- **Unit tests**: `tests/unit/test_review_plan.py::TestActionPlanValidation::test_docs_only_plan_with_empty_integration_promises_stays_valid`

## Integration tests

- `tests/integration/test_plan_contract_lints.py`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-PLAN-CONTRACT-LINTS",
  "title": "Plan-form lint: integration promises must be created by some step",
  "summary": "Add a new `model_validator(mode=\"after\")` on `ActionPlan` that rejects any plan whose `integration_tests` selector names a file absent from the union of all steps' `files`, with a directive error message naming the selector, the missing file, and the phrase \"add that file\". Mirrors the existing unit-side rule but without index ordering (integration tests run at session end).",
  "steps": [
    {
      "index": 1,
      "title": "Add the integration-promise validator and its first unit + integration tests",
      "files": [
        "src/ferova/review/plan.py",
        "tests/unit/test_review_plan.py",
        "tests/integration/test_plan_contract_lints.py"
      ],
      "action": "In `src/ferova/review/plan.py`, add a new `@model_validator(mode=\"after\")` method on `ActionPlan` (placed after `_promised_tests_are_created_by_the_plan`) named e.g. `_integration_promises_are_created_by_the_plan`. It must: (1) compute the union of every step's `files` across ALL steps (no index ordering — integration tests run at session end); (2) for each selector in `self.integration_tests`, split on `\"::\"` with `maxsplit=1` and take the file part; (3) if that file is not in the union, raise `ValueError` with a directive message that names the offending selector, the missing file path, and the phrase \"add that file\" (e.g. `f\"integration test {selector!r} is promised but no step creates {test_file!r} — add that file to a step's files (the per-step jail forbids writing files outside every contract)\"`). The validator must be a no-op when `integration_tests` is empty (vacuous pass). In `tests/unit/test_review_plan.py`, add `test_integration_promise_without_creating_step_is_rejected` (AC1): build a plan with one step whose `files` is `[\"src/ferova/feature.py\", \"tests/unit/test_feature.py\"]` and `unit_tests=[\"tests/unit/test_feature.py\"]`, and `integration_tests=[\"tests/integration/test_feature_e2e.py\"]` — assert `ValidationError` is raised and the message contains the selector, the missing file path, and \"add that file\". Create `tests/integration/test_plan_contract_lints.py` with one test that round-trips a violating plan through `render_plan_markdown` then `parse_plan_markdown` and asserts the same `ValidationError` with the directive message — this proves the lint fires on the committed-document path, not just direct construction.",
      "commit_message": "feat(review): reject integration promises no step creates",
      "done_when": "pytest tests/unit/test_review_plan.py::TestActionPlanValidation::test_integration_promise_without_creating_step_is_rejected passes and pytest tests/integration/test_plan_contract_lints.py passes",
      "unit_tests": [
        "tests/unit/test_review_plan.py::TestActionPlanValidation::test_integration_promise_without_creating_step_is_rejected"
      ]
    },
    {
      "index": 2,
      "title": "Add acceptance tests for the new rule (any-step creation + node-id resolution)",
      "files": [
        "tests/unit/test_review_plan.py"
      ],
      "action": "In `tests/unit/test_review_plan.py`, add two tests inside `TestActionPlanValidation`. (a) `test_integration_promise_created_by_any_step_is_accepted` (AC2): build a two-step plan where step 1 has `files=[\"src/ferova/feature.py\", \"tests/unit/test_feature.py\"]` and `unit_tests=[\"tests/unit/test_feature.py\"]`, and step 2 has `files=[\"tests/integration/test_feature_e2e.py\"]` and `unit_tests=[]` (docs-only exemption — but step 2 is not docs-only, so it must promise a unit test; use a trivial unit test like `[\"tests/unit/test_feature.py::test_smoke\"]` which already exists from step 1). Set `integration_tests=[\"tests/integration/test_feature_e2e.py::test_e2e\"]`. Assert the plan validates. (b) `test_integration_promise_node_id_resolves_file_part` (AC3): a single-step plan with `files=[\"src/ferova/feature.py\", \"tests/integration/test_feature_e2e.py\"]`, `unit_tests=[\"tests/integration/test_feature_e2e.py::test_smoke\"]`, and `integration_tests=[\"tests/integration/test_feature_e2e.py::test_e2e\"]` — assert it validates (the `::node` suffix is stripped before the file-part check).",
      "commit_message": "test(review): cover integration-promise acceptance paths",
      "done_when": "pytest tests/unit/test_review_plan.py::TestActionPlanValidation::test_integration_promise_created_by_any_step_is_accepted tests/unit/test_review_plan.py::TestActionPlanValidation::test_integration_promise_node_id_resolves_file_part passes",
      "unit_tests": [
        "tests/unit/test_review_plan.py::TestActionPlanValidation::test_integration_promise_created_by_any_step_is_accepted",
        "tests/unit/test_review_plan.py::TestActionPlanValidation::test_integration_promise_node_id_resolves_file_part"
      ]
    },
    {
      "index": 3,
      "title": "Add regression test for the docs-only exemption under the new rule",
      "files": [
        "tests/unit/test_review_plan.py"
      ],
      "action": "In `tests/unit/test_review_plan.py`, add `test_docs_only_plan_with_empty_integration_promises_stays_valid` (AC4): build a plan with a single docs-only step (`files=[\"docs/notes.md\"]`, `unit_tests=[]`) and `integration_tests=[]`. Assert the plan validates — the new rule must be vacuously satisfied on empty `integration_tests`, and the existing src-interlock exemption at `plan.py:186-192` must remain untouched.",
      "commit_message": "test(review): guard docs-only exemption under new lint",
      "done_when": "pytest tests/unit/test_review_plan.py::TestActionPlanValidation::test_docs_only_plan_with_empty_integration_promises_stays_valid passes",
      "unit_tests": [
        "tests/unit/test_review_plan.py::TestActionPlanValidation::test_docs_only_plan_with_empty_integration_promises_stays_valid"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_plan_contract_lints.py"
  ]
}
```
