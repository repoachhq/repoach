# SP-PLANNER-SELECTOR-CHECK — Mechanical selector verification in the Planner's refine loop

After ActionPlan schema validation succeeds inside the Planner session, additionally resolve every promised selector (step unit_tests + plan integration_tests) whose file exists at head: it must satisfy selector_present OR its node id must appear verbatim in the promising step's action text. Violations are rejected through the existing (None, reason) return so the existing _refine_prompt retry loop carries the directive message. Selectors whose file does not exist at head are exempt (the file itself is the step's deliverable).

## Step 1 — Add selector check to Planner's parse-retry loop

- **Files**: `src/ferova/review/planner.py`, `tests/unit/test_review_planner.py`
- **Action**: In src/ferova/review/planner.py, import selector_present from .spec_gate and add a module-level helper _check_promised_selectors(plan, repo_root) -> str | None that returns None when every promised selector (step.unit_tests + plan.integration_tests) either (a) has a file absent at head (exempt), (b) satisfies selector_present(repo_root, selector), or (c) has its node id (the substring after '::' if present, else the bare file path) appear verbatim in the promising step's action text. When any selector violates, return a directive message listing each offending selector and the two remedies ('make selector_present resolve it' / 'declare creation by naming the node id verbatim in the step action text'). Call _check_promised_selectors(plan, self._repo_root) immediately after _parse_and_validate returns a non-None plan in BOTH _plan_via_proxy and _plan_via_cc; on a non-empty return, treat it like any validation error (set last_error, continue the retry loop, do not return success). Add four module-level tests to tests/unit/test_review_planner.py: test_hallucinated_selector_in_existing_file_is_refined (build a plan whose step promises a nonexistent node id in an existing test file; assert the planner rejects it and the refine prompt carries the selector plus both remedies), test_resolved_selector_is_accepted (create the promised test file with the symbol; assert the plan is accepted), test_declared_creation_in_action_text_is_accepted (promise a nonexistent node id whose name appears verbatim in the step's action text; assert accepted), test_selector_in_new_file_is_exempt (promise a selector whose file does not exist at head; assert accepted).
- **Commit**: `feat(planner): verify promised selectors in refine loop`
- **Done when**: pytest tests/unit/test_review_planner.py -k 'selector' passes AND pytest tests/unit/test_review_planner.py passes
- **Unit tests**: `tests/unit/test_review_planner.py::test_hallucinated_selector_in_existing_file_is_refined`, `tests/unit/test_review_planner.py::test_resolved_selector_is_accepted`, `tests/unit/test_review_planner.py::test_declared_creation_in_action_text_is_accepted`, `tests/unit/test_review_planner.py::test_selector_in_new_file_is_exempt`

## Step 2 — Integration test for selector check end-to-end

- **Files**: `tests/integration/test_planner_selector_check.py`
- **Action**: Create tests/integration/test_planner_selector_check.py with one test test_planner_session_rejects_hallucinated_selector_in_existing_file that drives run_planner_session end-to-end with a fake AgentLoop emitting a plan whose step promises a nonexistent node id in an existing test file (e.g. tests/unit/test_review_planner.py::test_ghost_symbol). Assert outcome.written is False, outcome.error names the offending selector and both remedies, and no plan document was written under docs/plans/. Also assert that the same plan with the node id declared verbatim in the step's action text is accepted (outcome.written is True).
- **Commit**: `test(planner): integration coverage for selector check`
- **Done when**: pytest tests/integration/test_planner_selector_check.py passes
- **Unit tests**: `tests/integration/test_planner_selector_check.py::test_planner_session_rejects_hallucinated_selector_in_existing_file`

## Integration tests

- `tests/integration/test_planner_selector_check.py::test_planner_session_rejects_hallucinated_selector_in_existing_file`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-PLANNER-SELECTOR-CHECK",
  "title": "Mechanical selector verification in the Planner's refine loop",
  "summary": "After ActionPlan schema validation succeeds inside the Planner session, additionally resolve every promised selector (step unit_tests + plan integration_tests) whose file exists at head: it must satisfy selector_present OR its node id must appear verbatim in the promising step's action text. Violations are rejected through the existing (None, reason) return so the existing _refine_prompt retry loop carries the directive message. Selectors whose file does not exist at head are exempt (the file itself is the step's deliverable).",
  "steps": [
    {
      "index": 1,
      "title": "Add selector check to Planner's parse-retry loop",
      "files": [
        "src/ferova/review/planner.py",
        "tests/unit/test_review_planner.py"
      ],
      "action": "In src/ferova/review/planner.py, import selector_present from .spec_gate and add a module-level helper _check_promised_selectors(plan, repo_root) -> str | None that returns None when every promised selector (step.unit_tests + plan.integration_tests) either (a) has a file absent at head (exempt), (b) satisfies selector_present(repo_root, selector), or (c) has its node id (the substring after '::' if present, else the bare file path) appear verbatim in the promising step's action text. When any selector violates, return a directive message listing each offending selector and the two remedies ('make selector_present resolve it' / 'declare creation by naming the node id verbatim in the step action text'). Call _check_promised_selectors(plan, self._repo_root) immediately after _parse_and_validate returns a non-None plan in BOTH _plan_via_proxy and _plan_via_cc; on a non-empty return, treat it like any validation error (set last_error, continue the retry loop, do not return success). Add four module-level tests to tests/unit/test_review_planner.py: test_hallucinated_selector_in_existing_file_is_refined (build a plan whose step promises a nonexistent node id in an existing test file; assert the planner rejects it and the refine prompt carries the selector plus both remedies), test_resolved_selector_is_accepted (create the promised test file with the symbol; assert the plan is accepted), test_declared_creation_in_action_text_is_accepted (promise a nonexistent node id whose name appears verbatim in the step's action text; assert accepted), test_selector_in_new_file_is_exempt (promise a selector whose file does not exist at head; assert accepted).",
      "commit_message": "feat(planner): verify promised selectors in refine loop",
      "done_when": "pytest tests/unit/test_review_planner.py -k 'selector' passes AND pytest tests/unit/test_review_planner.py passes",
      "unit_tests": [
        "tests/unit/test_review_planner.py::test_hallucinated_selector_in_existing_file_is_refined",
        "tests/unit/test_review_planner.py::test_resolved_selector_is_accepted",
        "tests/unit/test_review_planner.py::test_declared_creation_in_action_text_is_accepted",
        "tests/unit/test_review_planner.py::test_selector_in_new_file_is_exempt"
      ]
    },
    {
      "index": 2,
      "title": "Integration test for selector check end-to-end",
      "files": [
        "tests/integration/test_planner_selector_check.py"
      ],
      "action": "Create tests/integration/test_planner_selector_check.py with one test test_planner_session_rejects_hallucinated_selector_in_existing_file that drives run_planner_session end-to-end with a fake AgentLoop emitting a plan whose step promises a nonexistent node id in an existing test file (e.g. tests/unit/test_review_planner.py::test_ghost_symbol). Assert outcome.written is False, outcome.error names the offending selector and both remedies, and no plan document was written under docs/plans/. Also assert that the same plan with the node id declared verbatim in the step's action text is accepted (outcome.written is True).",
      "commit_message": "test(planner): integration coverage for selector check",
      "done_when": "pytest tests/integration/test_planner_selector_check.py passes",
      "unit_tests": [
        "tests/integration/test_planner_selector_check.py::test_planner_session_rejects_hallucinated_selector_in_existing_file"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_planner_selector_check.py::test_planner_session_rejects_hallucinated_selector_in_existing_file"
  ]
}
```
